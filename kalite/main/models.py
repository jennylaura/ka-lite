import random
import uuid
from annoying.functions import get_object_or_None
from math import ceil
from datetime import datetime
from dateutil import relativedelta

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Sum

import settings
from securesync import engine
from securesync.models import SyncedModel, FacilityUser, Device
from settings import LOG as logging
from utils.django_utils import ExtendedModel
from utils.general import datediff, isnumeric


class VideoLog(SyncedModel):
    POINTS_PER_VIDEO = 750

    user = models.ForeignKey(FacilityUser, blank=True, null=True, db_index=True)
    youtube_id = models.CharField(max_length=20, db_index=True)
    total_seconds_watched = models.IntegerField(default=0)
    points = models.IntegerField(default=0)
    complete = models.BooleanField(default=False)
    completion_timestamp = models.DateTimeField(blank=True, null=True)
    completion_counter = models.IntegerField(blank=True, null=True)

    def __unicode__(self):
        return u"user=%s, youtube_id=%s, seconds=%d, points=%d%s" % (self.user, self.youtube_id, self.total_seconds_watched, self.points, " (completed)" if self.complete else "")

    class Meta:
        pass

    def save(self, *args, **kwargs):
        if not kwargs.get("imported", False):
            self.full_clean()

            # Compute learner status
            already_complete = self.complete
            self.complete = (self.points >= VideoLog.POINTS_PER_VIDEO)
            if not already_complete and self.complete:
                self.completion_timestamp = datetime.now()
                self.completion_counter = Device.get_own_device().get_counter()

            # Tell logins that they are still active (ignoring validation failures).
            #   TODO(bcipolli): Could log video information in the future.
            try:
                UserLog.update_user_activity(self.user, activity_type="login", update_datetime=(self.completion_timestamp or datetime.now()))
            except ValidationError as e:
                logging.error("Failed to update userlog during video: %s" % e)

        super(VideoLog, self).save(*args, **kwargs)

    def get_uuid(self, *args, **kwargs):
        assert self.user is not None and self.user.id is not None, "User ID required for get_uuid"
        assert self.youtube_id is not None, "Youtube ID required for get_uuid"

        namespace = uuid.UUID(self.user.id)
        return uuid.uuid5(namespace, self.youtube_id.encode("utf-8")).hex

    @staticmethod
    def get_points_for_user(user):
        return VideoLog.objects.filter(user=user).aggregate(Sum("points")).get("points__sum", 0) or 0

    @classmethod
    def calc_points(cls, seconds_watched, video_length):
        return ceil(float(seconds_watched) / video_length* VideoLog.POINTS_PER_VIDEO)

    @classmethod
    def update_video_log(cls, facility_user, youtube_id, total_seconds_watched, points=0, new_points=0):
        assert facility_user and youtube_id, "Updating a video log requires both a facility user and a YouTube ID"
        
        # retrieve the previous video log for this user for this video, or make one if there isn't already one
        (videolog, _) = cls.get_or_initialize(user=facility_user, youtube_id=youtube_id)
        
        # combine the previously watched counts with the new counts
        #
        # Set total_seconds_watched directly, rather than incrementally, for robustness
        #   as sometimes an update request fails, and we'd miss the time update!
        videolog.total_seconds_watched = total_seconds_watched  
        videolog.points = min(max(points, videolog.points + new_points), cls.POINTS_PER_VIDEO)
        
        # write the video log to the database, overwriting any old video log with the same ID
        # (and since the ID is computed from the user ID and YouTube ID, this will behave sanely)
        videolog.full_clean()
        videolog.save()

        return videolog


class ExerciseLog(SyncedModel):
    user = models.ForeignKey(FacilityUser, blank=True, null=True, db_index=True)
    exercise_id = models.CharField(max_length=100, db_index=True)
    streak_progress = models.IntegerField(default=0)
    attempts = models.IntegerField(default=0)
    points = models.IntegerField(default=0)
    complete = models.BooleanField(default=False)
    struggling = models.BooleanField(default=False)
    attempts_before_completion = models.IntegerField(blank=True, null=True)
    completion_timestamp = models.DateTimeField(blank=True, null=True)
    completion_counter = models.IntegerField(blank=True, null=True)

    def __unicode__(self):
        return u"user=%s, exercise_id=%s, points=%d%s" % (self.user, self.exercise_id, self.points, " (completed)" if self.complete else "")

    class Meta:
        pass

    def save(self, *args, **kwargs):
        if not kwargs.get("imported", False):
            self.full_clean()

            # Compute learner status
            if self.attempts > 20 and not self.complete:
                self.struggling = True
            already_complete = self.complete
            self.complete = (self.streak_progress >= 100)
            if not already_complete and self.complete:
                self.struggling = False
                self.completion_timestamp = datetime.now()
                self.completion_counter = Device.get_own_device().get_counter()
                self.attempts_before_completion = self.attempts

            # Tell logins that they are still active (ignoring validation failures).
            #   TODO(bcipolli): Could log exercise information in the future.
            try:
                UserLog.update_user_activity(self.user, activity_type="login", update_datetime=(self.completion_timestamp or datetime.now()))
            except ValidationError as e:
                logging.error("Failed to update userlog during exercise: %s" % e)
        super(ExerciseLog, self).save(*args, **kwargs)

    def get_uuid(self, *args, **kwargs):
        assert self.user is not None and self.user.id is not None, "User ID required for get_uuid"
        assert self.exercise_id is not None, "Exercise ID required for get_uuid"

        namespace = uuid.UUID(self.user.id)
        return uuid.uuid5(namespace, self.exercise_id.encode("utf-8")).hex

    @classmethod
    def calc_points(cls, basepoints, ncorrect=1, add_randomness=True):
        # This is duplicated in javascript, in kalite/static/js/exercises.js
        inc = 0
        for i in range(ncorrect):
            bumpprob = 100 * random.random()
            # If we're adding randomness, then we add
            # 50% more points 9% of the time,
            # 100% more points 1% of the time.
            bump = 1.0 + add_randomness * (0.5*(bumpprob >= 90) + 0.5*(bumpprob>=99))
            inc += basepoints * bump;
        return ceil(inc)

    @staticmethod
    def get_points_for_user(user):
        return ExerciseLog.objects.filter(user=user).aggregate(Sum("points")).get("points__sum", 0) or 0


class UserLogSummary(SyncedModel):
    """Like UserLogs, but summarized over a longer period of time.
    Also sync'd across devices.  Unique per user, device, activity_type, and time period."""
    minversion = "0.9.4"

    device = models.ForeignKey(Device, blank=False, null=False)
    user = models.ForeignKey(FacilityUser, blank=False, null=False, db_index=True)
    activity_type = models.IntegerField(blank=False, null=False)
    start_datetime = models.DateTimeField(blank=False, null=False)
    end_datetime = models.DateTimeField(blank=True, null=True)
    count = models.IntegerField(default=0, blank=False, null=False)
    total_seconds = models.IntegerField(default=0, blank=False, null=False)

    class Meta:
        pass

    def __unicode__(self):
        self.full_clean()  # make sure everything that has to be there, is there.
        return u"%d seconds over %d logins for %s/%s/%d, period %s to %s" % (self.total_seconds, self.count, self.device.name, self.user.username, self.activity_type, self.start_datetime, self.end_datetime)


    @classmethod
    def get_period_start_datetime(cls, log_time, summary_freq):
        """Periods can be: days, weeks, months, years.
        Days referenced from midnight on the current computer's clock.
        Weeks referenced from Monday morning @ 00:00:00am.
        Months and years follow from the above."""

        summary_freq_qty    = summary_freq[0]
        summary_freq_period = summary_freq[1].lower()
        base_time = log_time.replace(microsecond=0, second=0, minute=0, hour=0)

        if summary_freq_period in ["day", "days"]:
            assert summary_freq_qty == 1, "Days only supports 1"
            return base_time

        elif summary_freq_period in ["week", "weeks"]:
            assert summary_freq_qty == 1, "Weeks only supports 1"
            raise NotImplementedError("Still working to implement weeks.")

        elif summary_freq_period in ["month", "months"]:
            assert summary_freq_qty in [1,2,3,4,6], "Months only supports [1,2,3,4,6]"
            # Integer math makes this equation work as desired
            return base_time.replace(day=1, month=log_time.month / summary_freq_qty * summary_freq_qty)

        elif summary_freq_period in ["year", "years"]:
            assert summary_freq_qty in [1,2,3,4,6], "Years only supports 1"
            return base_time.replace(day=1, month=1)

        else:
            raise NotImplementedError("Unrecognized summary frequency period: %s" % summary_freq_period)


    @classmethod
    def get_period_end_datetime(cls, log_time, summary_freq):
        start_datetime = cls.get_period_start_datetime(log_time, summary_freq)
        summary_freq_qty    = summary_freq[0]
        summary_freq_period = summary_freq[1].lower()

        if summary_freq_period in ["day", "days"]:
            return start_datetime + relativedelta.relativedelta(days=summary_freq_qty) - relativedelta.relativedelta(seconds=1)

        elif summary_freq_period in ["week", "weeks"]:
            return start_datetime + relativedelta.relativedelta(days=7*summary_freq_qty) - relativedelta.relativedelta(seconds=1)

        elif summary_freq_period in ["month", "months"]:
            return start_datetime + relativedelta.relativedelta(months=summary_freq_qty) - relativedelta.relativedelta(seconds=1)

        elif summary_freq_period in ["year", "years"]:
            return start_datetime + relativedelta.relativedelta(years=summary_freq_qty) - relativedelta.relativedelta(seconds=1)

        else:
            raise NotImplementedError("Unrecognized summary frequency period: %s" % summary_freq_period)


    @classmethod
    def add_log_to_summary(cls, user_log, device=None):
        """Adds total_time to the appropriate user/device/activity's summary log."""

        assert user_log.end_datetime, "all log items must have an end_datetime to be saved here."
        assert user_log.total_seconds >= 0, "all log items must have a non-negative total_seconds to be saved here."
        device = device or Device.get_own_device()  # Must be done here, or install fails

        # Check for an existing object
        log_summary = cls.objects.filter(
            device=device,
            user=user_log.user,
            activity_type=user_log.activity_type,
            start_datetime__lte=user_log.end_datetime,
            end_datetime__gte=user_log.end_datetime,
        )
        assert log_summary.count() <= 1, "There should never be multiple summaries in the same time period/device/user/type combo"

        # Get (or create) the log item
        log_summary = log_summary[0] if log_summary.count() else cls(
            device=device,
            user=user_log.user,
            activity_type=user_log.activity_type,
            start_datetime=cls.get_period_start_datetime(user_log.end_datetime, settings.USER_LOG_SUMMARY_FREQUENCY),
            end_datetime=cls.get_period_end_datetime(user_log.end_datetime, settings.USER_LOG_SUMMARY_FREQUENCY),
            total_seconds=0,
            count=0,
        )

        logging.debug("Adding %d seconds for %s/%s/%d, period %s to %s" % (user_log.total_seconds, device.name, user_log.user.username, user_log.activity_type, log_summary.start_datetime, log_summary.end_datetime))

        # Add the latest info
        log_summary.total_seconds += user_log.total_seconds
        log_summary.count += 1
        log_summary.save()


class UserLog(ExtendedModel):  # Not sync'd, only summaries are
    """Detailed instances of user behavior.
    Currently not sync'd (only used for local detail reports).
    """

    # Currently, all activity is used just to update logged-in-time.
    KNOWN_TYPES={"login": 1, "coachreport": 2}
    minversion = "0.9.4"

    user = models.ForeignKey(FacilityUser, blank=False, null=False, db_index=True)
    activity_type = models.IntegerField(blank=False, null=False)
    start_datetime = models.DateTimeField(blank=False, null=False)
    last_active_datetime = models.DateTimeField(blank=False, null=False)
    end_datetime = models.DateTimeField(blank=True, null=True)
    total_seconds = models.IntegerField(blank=True, null=True)

    @staticmethod
    def is_enabled():
        return settings.USER_LOG_MAX_RECORDS_PER_USER != 0


    @transaction.commit_on_success
    def save(self, *args, **kwargs):
        """When this model is saved, check if the activity is ended.
        If so, compute total_seconds and update the corresponding summary log."""

        # Do nothing if the max # of records is zero
        # (i.e. this functionality is disabled)
        if not self.is_enabled():
            return

        if not self.start_datetime:
            raise ValidationError("start_datetime cannot be None")
        if self.last_active_datetime and self.start_datetime > self.last_active_datetime:
            raise ValidationError("UserLog date consistency check for start_datetime and last_active_datetime")

        if not self.end_datetime:
            # Conflict_resolution
            related_open_logs = UserLog.objects \
                .filter(user=self.user, activity_type=self.activity_type, end_datetime__isnull=True) \
                .exclude(pk=self.pk)
            for log in related_open_logs:
                log.end_datetime = datetime.now()
                log.save()

        elif not self.total_seconds:
            # Compute total_seconds, save to summary
            #   Note: only supports setting end_datetime once!
            self.full_clean()

            # The top computation is more lenient: user activity is just time logged in, literally.
            # The bottom computation is more strict: user activity is from start until the last "action"
            #   recorded--in the current case, that means from login until the last moment an exercise or
            #   video log was updated.
            #self.total_seconds = datediff(self.end_datetime, self.start_datetime, units="seconds")
            self.total_seconds = 0 if not self.last_active_datetime else datediff(self.last_active_datetime, self.start_datetime, units="seconds")

            # Confirm the result (output info first for easier debugging)
            logging.debug("%s: total time (%d): %d seconds" % (self.user.username, self.activity_type, self.total_seconds))
            if self.total_seconds < 0:
                raise ValidationError("Total learning time should always be non-negative.")

            # Save only completed log items to the UserLogSummary
            UserLogSummary.add_log_to_summary(self)

        # This is inefficient only if something goes awry.  Otherwise,
        #   this will really only do something on ADD.
        #   AND, if you're using recommended config (USER_LOG_MAX_RECORDS_PER_USER == 1),
        #   this will be very efficient.
        if settings.USER_LOG_MAX_RECORDS_PER_USER:  # Works for None, out of the box
            current_models = UserLog.objects.filter(user=self.user, activity_type=self.activity_type)
            if current_models.count() > settings.USER_LOG_MAX_RECORDS_PER_USER:
                # Unfortunately, could not do an aggregate delete when doing a
                #   slice in query
                to_discard = current_models \
                    .order_by("start_datetime")[0:current_models.count() - settings.USER_LOG_MAX_RECORDS_PER_USER]
                UserLog.objects.filter(pk__in=to_discard).delete()

        # Do it here, for efficiency of the above delete.
        super(UserLog, self).save(*args, **kwargs)


    def __unicode__(self):
        if self.end_datetime:
            return u"%s: logged in @ %s; for %s seconds"%(self.user.username,self.start_datetime, self.total_seconds)
        else:
            return u"%s: logged in @ %s; last active @ %s"%(self.user.username, self.start_datetime, self.last_active_datetime)


    @classmethod
    def get_activity_int(cls, activity_type):
        """Helper function converts from string or int to the underlying int"""

        if type(activity_type).__name__ in ["str", "unicode"]:
            if activity_type in cls.KNOWN_TYPES:
                return cls.KNOWN_TYPES[activity_type]
            else:
                raise Exception("Unrecognized activity type: %s" % activity_type)

        elif isnumeric(activity_type):
            return int(activity_type)

        else:
            raise Exception("Cannot convert requested activity_type to int")

    @classmethod
    def get_latest_open_log_or_None(cls, *args, **kwargs):
        assert not args
        assert "end_datetime" not in kwargs

        logs = cls.objects \
            .filter(end_datetime__isnull=True, **kwargs) \
            .order_by("-last_active_datetime")
        return None if not logs else logs[0]

    @classmethod
    def begin_user_activity(cls, user, activity_type="login", start_datetime=None):
        """Helper function to create a user activity log entry."""

        # Do nothing if the max # of records is zero
        # (i.e. this functionality is disabled)
        if not cls.is_enabled():
            return

        if not user:
            raise ValidationError("A valid user must always be specified.")
        if not start_datetime:  # must be done outside the function header (else becomes static)
            start_datetime = datetime.now()
        activity_type = cls.get_activity_int(activity_type)

        cur_log = cls.get_latest_open_log_or_None(user=user, activity_type=activity_type)
        if cur_log:
            # Seems we're logging in without logging out of the previous.
            #   Best thing to do is simulate a login
            #   at the previous last update time.
            #
            # Note: this can be a recursive call
            logging.warn("%s: had to END activity on a begin(%d) @ %s" % (user.username, activity_type, start_datetime))
            cls.end_user_activity(user=user, activity_type=activity_type, end_datetime=cur_log.last_active_datetime)
            cur_log = None

        # Create a new entry
        logging.debug("%s: BEGIN activity(%d) @ %s"%(user.username, activity_type, start_datetime))
        cur_log = cls(user=user, activity_type=activity_type, start_datetime=start_datetime, last_active_datetime=start_datetime)
        cur_log.save()

        return cur_log


    @classmethod
    def update_user_activity(cls, user, activity_type="login", update_datetime=None):
        """Helper function to update an existing user activity log entry."""

        # Do nothing if the max # of records is zero
        # (i.e. this functionality is disabled)
        if not cls.is_enabled():
            return

        if not user:
            raise ValidationError("A valid user must always be specified.")
        if not update_datetime:  # must be done outside the function header (else becomes static)
            update_datetime = datetime.now()
        activity_type = cls.get_activity_int(activity_type)

        cur_log = cls.get_latest_open_log_or_None(user=user, activity_type=activity_type)
        if cur_log:
            # How could you start after you updated??
            if cur_log.start_datetime > update_datetime:
                raise ValidationError("Update time must always be later than the login time.")
        else:
            # No unstopped starts.  Start should have been called first!
            logging.warn("%s: Had to create a user log entry on an UPDATE(%d)! @ %s"%(user.username, activity_type, update_datetime))
            cur_log = cls.begin_user_activity(user=user, activity_type=activity_type, start_datetime=update_datetime)

        logging.debug("%s: UPDATE activity (%d) @ %s"%(user.username,activity_type,update_datetime))
        cur_log.last_active_datetime = update_datetime
        cur_log.save()
        return cur_log


    @classmethod
    def end_user_activity(cls, user, activity_type="login", end_datetime=None):
        """Helper function to complete an existing user activity log entry."""

        # Do nothing if the max # of records is zero
        # (i.e. this functionality is disabled)
        if not cls.is_enabled():
            return

        if not user:
            raise ValidationError("A valid user must always be specified.")
        if not end_datetime:  # must be done outside the function header (else becomes static)
            end_datetime = datetime.now()
        activity_type = cls.get_activity_int(activity_type)

        cur_log = cls.get_latest_open_log_or_None(user=user, activity_type=activity_type)
        if cur_log:
            # How could you start after you ended??
            if cur_log.start_datetime > end_datetime:
                raise ValidationError("Update time must always be later than the login time.")
        else:
            # No unstopped starts.  Start should have been called first!
            logging.warn("%s: Had to create a user log entry, but STOPPING('%d')! @ %s"%(user.username, activity_type, end_datetime))
            cur_log = cls.begin_user_activity(user=user, activity_type=activity_type, start_datetime=end_datetime)

        logging.debug("%s: Logging LOGOUT activity @ %s"%(user.username, end_datetime))
        cur_log.end_datetime = end_datetime
        cur_log.save()  # total-seconds will be computed here.
        return cur_log


class VideoFile(ExtendedModel):
    youtube_id = models.CharField(max_length=20, primary_key=True)
    flagged_for_download = models.BooleanField(default=False)
    flagged_for_subtitle_download = models.BooleanField(default=False)
    download_in_progress = models.BooleanField(default=False)
    subtitle_download_in_progress = models.BooleanField(default=False)
    priority = models.IntegerField(default=0)
    percent_complete = models.IntegerField(default=0)
    subtitles_downloaded = models.BooleanField(default=False)
    cancel_download = models.BooleanField(default=False)

    class Meta:
        ordering = ["priority", "youtube_id"]


class LanguagePack(ExtendedModel):
    lang_id = models.CharField(max_length=5, primary_key=True)
    lang_version = models.CharField(max_length=5)
    software_version = models.CharField(max_length=12)
    lang_name = models.CharField(max_length=30)


engine.add_syncing_models([VideoLog, ExerciseLog, UserLogSummary])
