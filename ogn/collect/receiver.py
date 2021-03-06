from sqlalchemy.sql import func, null
from sqlalchemy.sql.functions import coalesce
from sqlalchemy import and_, or_

from celery.utils.log import get_task_logger

from ogn.model import Receiver, ReceiverBeacon
from ogn.utils import get_country_code
from ogn.collect.celery import app

logger = get_task_logger(__name__)


@app.task
def update_receivers():
    """Update the receiver table."""
    # get the timestamp of last update
    last_update_query = app.session.query(coalesce(func.max(Receiver.lastseen), '2015-01-01 00:00:00').label('last_entry'))
    last_update = last_update_query.one().last_entry

    # get last receiver beacons since last update
    last_receiver_beacon_sq = app.session.query(ReceiverBeacon.name,
                                                func.max(ReceiverBeacon.timestamp).label('lastseen')) \
                                         .filter(ReceiverBeacon.timestamp >= last_update) \
                                         .group_by(ReceiverBeacon.name) \
                                         .subquery()

    receivers_to_update = app.session.query(ReceiverBeacon.name,
                                            ReceiverBeacon.latitude,
                                            ReceiverBeacon.longitude,
                                            ReceiverBeacon.altitude,
                                            last_receiver_beacon_sq.columns.lastseen,
                                            ReceiverBeacon.version,
                                            ReceiverBeacon.platform) \
                                     .filter(and_(ReceiverBeacon.name == last_receiver_beacon_sq.columns.name,
                                                  ReceiverBeacon.timestamp == last_receiver_beacon_sq.columns.lastseen)) \
                                     .subquery()

    # set country code to None if lat or lon changed
    count = app.session.query(Receiver) \
                       .filter(and_(Receiver.name == receivers_to_update.columns.name,
                                    or_(Receiver.latitude != receivers_to_update.columns.latitude,
                                        Receiver.longitude != receivers_to_update.columns.longitude))) \
                       .update({"latitude": receivers_to_update.columns.latitude,
                                "longitude": receivers_to_update.columns.longitude,
                                "country_code": null()})

    logger.info("Count of receivers who changed lat or lon: {}".format(count))

    # update lastseen of known receivers
    count = app.session.query(Receiver) \
                       .filter(Receiver.name == receivers_to_update.columns.name) \
                       .update({"altitude": receivers_to_update.columns.altitude,
                                "lastseen": receivers_to_update.columns.lastseen,
                                "version": receivers_to_update.columns.version,
                                "platform": receivers_to_update.columns.platform})

    logger.info("Count of receivers who where updated: {}".format(count))

    # add new receivers
    empty_sq = app.session.query(ReceiverBeacon.name,
                                 ReceiverBeacon.latitude,
                                 ReceiverBeacon.longitude,
                                 ReceiverBeacon.altitude,
                                 last_receiver_beacon_sq.columns.lastseen,
                                 ReceiverBeacon.version, ReceiverBeacon.platform) \
                          .filter(and_(ReceiverBeacon.name == last_receiver_beacon_sq.columns.name,
                                       ReceiverBeacon.timestamp == last_receiver_beacon_sq.columns.lastseen)) \
                          .outerjoin(Receiver, Receiver.name == ReceiverBeacon.name) \
                          .filter(Receiver.name == null()) \
                          .order_by(ReceiverBeacon.name)

    for receiver_beacon in empty_sq.all():
        receiver = Receiver()
        receiver.name = receiver_beacon.name
        receiver.latitude = receiver_beacon.latitude
        receiver.longitude = receiver_beacon.longitude
        receiver.altitude = receiver_beacon.altitude
        receiver.firstseen = None
        receiver.lastseen = receiver_beacon.lastseen
        receiver.version = receiver_beacon.version
        receiver.platform = receiver_beacon.platform

        app.session.add(receiver)
        logger.info("{} added".format(receiver.name))

    # update firstseen if None
    firstseen_null_query = app.session.query(Receiver.name,
                                             func.min(ReceiverBeacon.timestamp).label('firstseen')) \
                                      .filter(Receiver.firstseen == null()) \
                                      .join(ReceiverBeacon, Receiver.name == ReceiverBeacon.name) \
                                      .group_by(Receiver.name) \
                                      .subquery()

    count = app.session.query(Receiver) \
                       .filter(Receiver.name == firstseen_null_query.columns.name) \
                       .update({'firstseen': firstseen_null_query.columns.firstseen})
    logger.info("Total: {} receivers added".format(count))

    # update country code if None
    unknown_country_query = app.session.query(Receiver) \
                                       .filter(Receiver.country_code == null()) \
                                       .order_by(Receiver.name)

    for receiver in unknown_country_query.all():
        receiver.country_code = get_country_code(receiver.latitude, receiver.longitude)
        if receiver.country_code is not None:
            logger.info("Updated country_code for {} to {}".format(receiver.name, receiver.country_code))

    app.session.commit()
