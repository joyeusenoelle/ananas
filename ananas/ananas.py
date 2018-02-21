import os, sys, re, time, threading, _thread
import configparser, inspect, getpass, traceback
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import mastodon
from mastodon import Mastodon, StreamListener
import ananas.mastodon_patch

# TODO: Polish up sample bots for distribution (and real use!)
# TODO: Final pass on code quality and commenting
# TODO: Write up documentation
# TODO: Wrap up in packaging for pypi!!!

###
# Decorators
###

def reply(f):
    """ Mark f as a handler for mention notifications. """
    f.reply = True
    return f

def error_reporter(f):
    f.error_reporter = True
    return f

def interval(seconds):
    def wrapper(f):
        f.interval = seconds
        return f
    return wrapper

def schedule(**kwargs):
    def wrapper(f):
        if (not hasattr(f, "schedule")):
            f.schedule = [kwargs]
        else:
            f.schedule.append(kwargs)
        return f
    return wrapper

def hourly(minute=0):
    return schedule(minute=minute)

def daily(hour=0, minute=0):
    return schedule(hour=hour, minute=minute)

# Utilities

def total_seconds(dt):
    """Returns the total number of seconds in a timedelta."""
    return dt.seconds + dt.days * 24 * 60 * 60

def interval_next(f, t = datetime.now(), tLast = datetime.now()):
    """
    Calculate the number of seconds from now until the function should next run.
    This function handles both cron-like and interval-like scheduling via the
    following:
     ∗ If no interval and no schedule are specified, return 0
     ∗ If an interval is specified but no schedule, return the number of seconds
       from <t> until <interval> has passed since <tLast> or 0 if it's overdue.
     ∗ If a schedule is passed but no interval, figure out when next to run by
       parsing the schedule according to the following rules:
         ∗ If all of second, minute, hour, day_of_week/day_of_month, month, year
           are specified, then the time to run is singular and the function will
           run only once at that time. If it has not happened yet, return the
           number of seconds from <t> until that time, otherwise return -1.
         ∗ If one or more are unspecified, then they are treated as open slots.
           return the number of seconds from <t> until the time next fits within
           the specified constraints, or if it never will again return -1.
             ∗ Only one of day_of_week and day_of_month may be specified. if both
               are specified, then day_of_month is used and day_of_week is ignored.
         ∗ If all are unspecified treat it as having no schedule specified
     ∗ If both a schedule and an interval are specified, TODO but it should do
       something along the lines of finding the next multiple of interval from tLast 
       that fits the schedule spec and returning the number of seconds until then.

    NOTE: If the time until the next event is greater than an hour in the
    future, this function will return the number of seconds until the top of the
    next hour (1-3600). Be sure to continue checking until this function
    returns 0.
    """
    has_interval = hasattr(f, "interval")
    has_schedule = hasattr(f, "schedule")

    if (not has_interval and not has_schedule):
        return 0
    if (has_interval and not has_schedule):
        tNext = tLast + timedelta(seconds = f.interval)
        return max(total_seconds(tNext - t), 0)
    if (has_schedule): # and not has_interval):
        interval_min = 3600
        for s in f.schedule:
            interval = schedule_next(s, t) 
            if interval < interval_min:
                interval_min = interval
        return interval_min

def schedule_next(schedule, t):
    # Operate in day-of-month mode unless day-of-week is specified
    use_day_of_week = "day_of_week" in schedule

    spec = [schedule.get("hour", -1),
            (schedule.get("day_of_week", -1) if use_day_of_week
             else schedule.get("day_of_month", -1)),
            schedule.get("month", -1),
            schedule.get("year", -1)]
    now = [t.hour,
           t.weekday() if use_day_of_week else t.day,
           t.month,
           t.year]
    matches_hour_spec = all(spec[slot] == -1 or now[slot] == spec[slot] for slot in range(4))

    # Note that if decorator didn't specify second, we assume 0, we don't pattern match.
    spec = [schedule.get("second", 0),
            schedule.get("minute", -1)]
    now = [t.second,
           t.minute]
    now_s = 60 * now[1] + now[0]

    if matches_hour_spec:
        # Wait until minute:second matches time spec
        if spec[1] == -1:
            if now[0] < spec[0]: return spec[0] - now[0]
            elif now[0] > spec[0]: return 60 - (now[0] - spec[0])
            else: return 0
        else:
            spec_s = 60 * spec[1] + spec[0]
            if (now_s <= spec_s): return spec_s - now_s
            #else: fallthrough
    # else:
    # Wait until top of next hour
    return 3600 - now_s

class HTMLTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = ""
    def handle_data(self, data):
        self.text += data;

def html_strip_tags(html_str, linebreaks=None):
    linebreaks = False if linebreaks == None else bool(linebreaks)
    if linebreaks:
        html_str = re.sub(r"<br([^>]*)>",
                          "\n<br\1>",
                          html_str)
        html_str = re.sub(r"<p([^>]*)>",
                          "\n<p\1>",
                          html_str)
    parser = HTMLTextParser()
    parser.feed(html_str)
    return parser.text

class PineappleBot(StreamListener):
    """
    Main bot class
    We subclass StreamListener so that we can use it as its own callback
    """

    INITIALIZING = 0
    STARTING = 1
    RUNNING = 2
    STOPPING = 3

    class Config(dict):
        def __init__(self, bot, filename): 
            dict.__init__(self)
            self._filename = filename
            self._cfg = configparser.ConfigParser()
            self._bot = bot
        def __getattr__(self, key): return self[key]
        def __setattr__(self, key, value): self[key] = value
        def __delattr(self, key): del self[key]

        def load(self, name=None):
            """ Load section <name> from the config file into this object,
            replacing/overwriting any values currently cached in here."""
            if (name != None):
                self._name = name

            self._cfg.read(self._filename)
            if (name not in self._cfg.sections()): 
                self._bot.log("config", "Section {} not in {}, aborting.".format(self._name, self._filename))
                return False
            self._bot.log("config", "Loading configuration from {}".format(self._filename))
            self.update(self._cfg[self._name])
            return True

        def save(self):
            """ Save back out to the config file. """
            self._cfg.read(self._filename)
            for attr, value in self.items():
                if attr[0] != '_' and not (attr in self._cfg["DEFAULT"] and self._cfg["DEFAULT"][attr] == value):
                    self._cfg[self._name][attr] = str(value)
            self._bot.log("config", "Saving configuration to {}...".format(self._filename))
            with open(self._filename, 'w') as cfgfile:
                self._cfg.write(cfgfile)
            self._bot.log("config", "Done.")
            return True

    def __init__(self, cfgname, name=None, log_to_stderr=True, interactive=False, verbose=False):
        if (name is None): name = self.__class__.__name__
        self.name = name
        self.state = PineappleBot.INITIALIZING

        self.alive = threading.Condition()
        self.threads = []
        self.reply_funcs = []
        self.report_funcs = []

        self.mastodon = None
        self.stream = None
        self.interactive = interactive
        self.verbose = verbose

        self.log_to_stderr = log_to_stderr
        self.log_name = self.name + ".log"
        self.log_file = open(self.log_name, "a")

        self.config = PineappleBot.Config(self, cfgname)
        self.init() # Call user init to initialize bot-specific properties to default values
        if not self.config.load(self.name): return
        if not self.login(): return

        self.startup()

    def log(self, id, msg):
        if (id == None): id = self.name
        else: id = self.name + "." + id
        ts = datetime.now()
        msg_f = "[{0:%Y-%m-%d %H:%M:%S}] {1}: {2}".format(ts, id, msg)

        if self.log_file.closed or self.log_to_stderr: print(msg_f, file=sys.stderr)
        elif not self.log_file.closed: print(msg_f, file=self.log_file)

    def startup(self):
        self.state = PineappleBot.STARTING
        self.log(None, "Starting {0} {1}".format(self.__class__.__name__, self.name))

        try:
            self.start()
        except Exception as e:
            self.log(None, "Fatal exception: {}\n{}".format(repr(e), traceback.format_exc()))
            return
        
        def interval_threadproc(f):
            self.log(f.__name__, "Started")
            t = datetime.now()
            tLast = t
            while (True):
                self.alive.acquire()
                t = datetime.now()
                interval = interval_next(f, t, tLast)

                if (interval == 0):
                    try:
                        f()
                    except Exception as e:
                        error = "Fatal exception: {}\n{}".format(repr(e), traceback.format_exc())
                        self.report_error(error, f.__name__)
                        self.alive.release()
                        return 0

                    t = datetime.now()
                    interval = interval_next(f, t, t)

                if self.verbose: self.log(f.__name__ + ".debug", "Next wait interval: {}s".format(interval))
                tLast = t
                self.alive.wait(max(interval, 1))
                if (self.state == PineappleBot.STOPPING):
                    self.alive.release()
                    self.log(f.__name__, "Shutting down")
                    return 0
                else:
                    self.alive.release()

        for fname, f in inspect.getmembers(self, predicate=inspect.ismethod):
            if hasattr(f, "interval") or hasattr(f, "schedule"):
                t = threading.Thread(args=(f,), target=interval_threadproc)
                t.start()
                self.threads.append(t)

            if hasattr(f, "reply"):
                self.reply_funcs.append(f)

            if hasattr(f, "error_reporter"):
                self.report_funcs.append(f)

        if len(self.reply_funcs) > 0:
            self.stream = self.mastodon.user_stream(self, async=True)
        self.state = PineappleBot.RUNNING
        self.log(None, "Startup complete.")

    def shutdown(self):
        self.alive.acquire()
        self.state = PineappleBot.STOPPING
        self.log(None, "Stopping {0} {1}".format(self.__class__.__name__, self.name))
        self.alive.notify_all()
        self.alive.release()

        if self.stream: self.stream.close()

        self.stop()
        self.config.save()

        self.log_file.close()

    def login(self):
        if self.interactive and not self.interactive_login(): 
            self.log("api", "Interactive login failed, exiting.")
            return False
        elif "domain" not in self.config:
            self.log("api", "No domain set in config and interactive = False, exiting.")
            return False
        elif "client_id" not in self.config:
            self.log("api", "No client id set in config and interactive = False, exiting.")
            return False
        elif "client_secret" not in self.config:
            self.log("api", "No client secret set in config and interactive = False, exiting.")
            return False
        elif "access_token" not in self.config:
            self.log("api", "No access key set in config and interactive = False, exiting.")
            return False

        self.mastodon = Mastodon(client_id = self.config.client_id, 
                                  client_secret = self.config.client_secret, 
                                  access_token = self.config.access_token, 
                                  api_base_url = self.config.domain)
                                  #debug_requests = True)
        self.log("debug", "Mastodon IS patched" if Mastodon._patched else "Mastodon IS NOT patched!!!")
        return True

    def interactive_login(self):
        try:
            if (not hasattr(self, "domain")):
                domain = input("{0}: Enter the instance domain [mastodon.social]: ".format(self.name))
                domain = domain.strip()
                if (domain == ""): domain = "mastodon.social"
                self.config.domain = domain
            # We have to generate these two together, if just one is 
            # specified in the config file it's no good.
            if (not hasattr(self, "client_id") or not hasattr(self, "client_secret")):
                client_name = input("{0}: Enter a name for this bot or service [{0}]: ".format(self.name))
                client_name = client_name.strip()
                if (client_name == ""): client_name = self.name
                self.config.client_id, self.config.client_secret = Mastodon.create_app(client_name, 
                        api_base_url="https://"+self.config.domain)
                # TODO handle failure
                self.config.save()
            if (not hasattr(self, "access_token")):
                email = input("{0}: Enter the account email: ".format(self.name))
                email = email.strip()
                password = getpass.getpass("{0}: Enter the account password: ".format(self.name))
                try:
                    mastodon = Mastodon(client_id = self.config.client_id,
                                        client_secret = self.config.client_secret,
                                        api_base_url = "https://"+self.config.domain)
                    self.config.access_token = mastodon.log_in(email, password)
                    self.config.save()
                except ValueError as e:
                    self.log("login", "Could not authenticate with {0} as '{1}':"
                             .format(self.config.domain, email))
                    self.log("login", str(e))
                    self.log("debug", "using the password {0}".format(password))
                    return False
            return True
        except KeyboardInterrupt:
            return False

    @error_reporter
    def default_report_handler(self, error):
        if self.mastodon and "admin" in self.config:
            self.mastodon.status_post(("@{} ERROR REPORT from {}:\n{}"
                                       .format(self.config.admin, self.name, error))[:500],
                                      visibility="direct")

    def report_error(self, error, location=None):
        """Report an error that occurred during bot operations. The default
        handler tries to DM the bot admin, if one is set, but more handlers can
        be added by using the @error_reporter decorator."""
        if location == None: location = inspect.stack()[1][3]
        self.log(location, error)
        for f in self.report_funcs:
            f(error)

    def on_notification(self, notif):
        self.log("debug", "Got a {} from {} at {}".format(notif["type"], notif["account"]["username"], notif["created_at"]))
        if (notif["type"] == "mention"):
            for f in self.reply_funcs:
                try:
                    f(notif["status"], notif["account"])
                except Exception as e:
                    error = "Fatal exception: {}\n{}".format(repr(e), traceback.format_exc())
                    self.report_error(error, f.__name__)

    # defaults, should be replaced by concrete bots with actual implementations
    # (if necessary, anyway)
    def init(self):
        pass
    def start(self):
        pass
    def stop(self):
        pass

# Exceptions

class ConfigurationError(Exception):
    pass
