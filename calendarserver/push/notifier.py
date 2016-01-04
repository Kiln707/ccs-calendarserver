##
# Copyright (c) 2005-2015 Apple Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##

"""
Notification framework for Calendar Server
"""

from twext.enterprise.dal.record import fromTable
from twext.enterprise.dal.syntax import Delete, Select, Parameter
from twext.enterprise.jobs.jobitem import JobItem
from twext.enterprise.jobs.workitem import WorkItem, WORK_PRIORITY_HIGH, \
    WORK_WEIGHT_1
from twext.python.log import Logger

from twisted.internet.defer import inlineCallbacks

from txdav.common.datastore.sql_tables import schema
from txdav.idav import IStoreNotifierFactory, IStoreNotifier

from zope.interface.declarations import implements

import datetime

from calendarserver.push.ipush import PushPriority

log = Logger()



class PushNotificationWork(WorkItem, fromTable(schema.PUSH_NOTIFICATION_WORK)):

    group = property(lambda self: (self.table.PUSH_ID == self.pushID))
    default_priority = WORK_PRIORITY_HIGH
    default_weight = WORK_WEIGHT_1

    @inlineCallbacks
    def doWork(self):

        # Find all work items with the same push ID and find the highest
        # priority.  Delete matching work items.
        results = (yield Select(
            [self.table.WORK_ID, self.table.JOB_ID, self.table.PUSH_PRIORITY],
            From=self.table, Where=self.table.PUSH_ID == self.pushID).on(
            self.transaction))

        maxPriority = self.pushPriority

        # If there are other enqueued work items for this push ID, find the
        # highest priority one and use that value. Note that L{results} will
        # not contain this work item as job processing behavior will have already
        # deleted it. So we need to make sure the max priority calculation includes
        # this one.
        if results:
            workIDs, jobIDs, priorities = zip(*results)
            maxPriority = max(priorities + (self.pushPriority,))

            # Delete the work items and jobs we selected - deleting the job will ensure that there are no
            # orphaned" jobs left in the job queue which would otherwise get to run at some later point,
            # though not do anything because there is no related work item.
            yield Delete(
                From=self.table,
                Where=self.table.WORK_ID.In(Parameter("workIDs", len(workIDs)))
            ).on(self.transaction, workIDs=workIDs)
            yield Delete(
                From=JobItem.table, #@UndefinedVariable
                Where=JobItem.jobID.In(Parameter("jobIDs", len(jobIDs))) #@UndefinedVariable
            ).on(self.transaction, jobIDs=jobIDs)

        pushDistributor = self.transaction._pushDistributor
        if pushDistributor is not None:
            # Convert the integer priority value back into a constant
            priority = PushPriority.lookupByValue(maxPriority)
            yield pushDistributor.enqueue(self.transaction, self.pushID, priority=priority)



# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
# Classes used within calendarserver itself
#

class Notifier(object):
    """
    Provides a hook for sending change notifications to the
    L{NotifierFactory}.
    """
    log = Logger()

    implements(IStoreNotifier)

    def __init__(self, notifierFactory, storeObject):
        self._notifierFactory = notifierFactory
        self._storeObject = storeObject
        self._notify = True


    def enableNotify(self, arg):
        self.log.debug("enableNotify: {id}", id=self._ids['default'][1])
        self._notify = True


    def disableNotify(self):
        self.log.debug("disableNotify: {id}", id=self._ids['default'][1])
        self._notify = False


    @inlineCallbacks
    def notify(self, txn, priority=PushPriority.high):
        """
        Send the notification. For a home object we just push using the home id. For a home
        child we push both the owner home id and the owned home child id.

        @param txn: The transaction to create the work item with
        @type txn: L{CommonStoreTransaction}
        @param priority: the priority level
        @type priority: L{PushPriority}
        """
        # Push ids from the store objects are a tuple of (prefix, name,) and we need to compose that
        # into a single token.
        ids = (self._storeObject.notifierID(),)

        # For resources that are children of a home, we need to add the home id too.
        if hasattr(self._storeObject, "parentNotifierID"):
            ids += (self._storeObject.parentNotifierID(),)

        for prefix, id in ids:
            if self._notify:
                self.log.debug(
                    "Notifications are enabled: {obj} {prefix}/{id} priority={priority}",
                    obj=self._storeObject, prefix=prefix, id=id, priority=priority.value
                )
                yield self._notifierFactory.send(
                    prefix, id, txn,
                    priority=priority)
            else:
                self.log.debug(
                    "Skipping notification for: %{obj} {prefix}/{id}",
                    obj=self._storeObject, prefix=prefix, id=id,
                )


    def clone(self, storeObject):
        return self.__class__(self._notifierFactory, storeObject)


    def nodeName(self):
        """
        The pushkey is the notifier id of the home collection for home and owned home child objects. For
        a shared home child, the push key is the notifier if of the owner's home child.
        """
        if hasattr(self._storeObject, "ownerHome"):
            if self._storeObject.owned():
                prefix, id = self._storeObject.ownerHome().notifierID()
            else:
                prefix, id = self._storeObject.notifierID()
        else:
            prefix, id = self._storeObject.notifierID()
        return self._notifierFactory.pushKeyForId(prefix, id)



class NotifierFactory(object):
    """
    Notifier Factory

    Creates Notifier instances and forwards notifications from them to the
    work queue.
    """
    log = Logger()

    implements(IStoreNotifierFactory)

    def __init__(self, hostname, coalesceSeconds, reactor=None):
        self.store = None   # Initialized after the store is created
        self.hostname = hostname
        self.coalesceSeconds = coalesceSeconds

        if reactor is None:
            from twisted.internet import reactor
        self.reactor = reactor


    @inlineCallbacks
    def send(self, prefix, id, txn, priority=PushPriority.high):
        """
        Enqueue a push notification work item on the provided transaction.
        """
        yield txn.enqueue(
            PushNotificationWork,
            pushID=self.pushKeyForId(prefix, id),
            notBefore=datetime.datetime.utcnow() + datetime.timedelta(seconds=self.coalesceSeconds),
            pushPriority=priority.value
        )


    def newNotifier(self, storeObject):
        return Notifier(self, storeObject)


    def pushKeyForId(self, prefix, id):
        key = "/%s/%s/%s/" % (prefix, self.hostname, id)
        return key[:255]



def getPubSubAPSConfiguration(notifierID, config):
    """
    Returns the Apple push notification settings specific to the pushKey
    """
    try:
        protocol, ignored = notifierID
    except ValueError:
        # id has no protocol, so we can't look up APS config
        return None

    # If we are directly talking to apple push, advertise those settings
    applePushSettings = config.Notifications.Services.APNS
    if applePushSettings.Enabled:
        settings = {}
        settings["APSBundleID"] = applePushSettings[protocol]["Topic"]
        if config.EnableSSL:
            url = "https://%s:%s/%s" % (
                config.ServerHostName, config.SSLPort,
                applePushSettings.SubscriptionURL)
        else:
            url = "http://%s:%s/%s" % (
                config.ServerHostName, config.HTTPPort,
                applePushSettings.SubscriptionURL)
        settings["SubscriptionURL"] = url
        settings["SubscriptionRefreshIntervalSeconds"] = applePushSettings.SubscriptionRefreshIntervalSeconds
        settings["APSEnvironment"] = applePushSettings.Environment
        return settings

    return None



class PushDistributor(object):
    """
    Distributes notifications to the protocol-specific subservices
    """

    def __init__(self, observers):
        """
        @param observers: the list of observers to distribute pushKeys to
        @type observers: C{list}
        """
        # TODO: add an IPushObservers interface?
        self.observers = observers


    @inlineCallbacks
    def enqueue(self, transaction, pushKey, priority=PushPriority.high):
        """
        Pass along enqueued pushKey to any observers

        @param transaction: a transaction to use, if needed
        @type transaction: L{CommonStoreTransaction}

        @param pushKey: the push key to distribute to the observers
        @type pushKey: C{str}

        @param priority: the priority level
        @type priority: L{PushPriority}
        """
        for observer in self.observers:
            yield observer.enqueue(
                transaction, pushKey,
                dataChangedTimestamp=None, priority=priority)
