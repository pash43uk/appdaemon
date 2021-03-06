import asyncio
import datetime
import traceback

import appdaemon.scheduler as scheduler
import appdaemon.utils as utils
from appdaemon.appdaemon import AppDaemon


class Utility:

    def __init__(self, ad: AppDaemon):

        self.AD = ad
        self.stopping = False
        self.logger = ad.logging.get_child("_utility")

    def stop(self):
        self.logger.debug("stop() called for utility")
        self.stopping = True

    async def loop(self):

        #
        # Setup
        #

        await self.AD.threading.init_admin_stats()
        await self.AD.threading.create_initial_threads()


        #
        # Wait for all plugins to initialize
        #

        await self.AD.plugins.wait_for_plugins()

        # Check if we need to bail due to missing metadata

        if self.AD.plugins.required_meta_check() is False:
            if self.AD.stop_function is not None:
                self.AD.stop_function()
            else:
                self.stop()

        if not self.stopping:

            #
            # All plugins are loaded and we have initial state
            # We also have metadata so we can initialise the scheduler
            #

            self.AD.sched = scheduler.Scheduler(self.AD)

            # Create timer loop

            self.logger.debug("Starting timer loop")

            self.AD.loop.create_task(self.AD.sched.loop())

            if self.AD.apps is True:
                self.logger.debug("Reading Apps")

                await self.AD.app_management.check_app_updates()

                self.logger.info("App initialization complete")
                #
                # Fire APPD Started Event
                #
                await self.AD.events.process_event("global", {"event_type": "appd_started", "data": {}})

            self.booted = await self.AD.sched.get_now()
            await self.AD.state.add_entity("admin", "sensor.appdaemon_version", utils.__version__)
            await self.AD.state.add_entity("admin", "sensor.appdaemon_uptime", str(datetime.timedelta(0)))
            await self.AD.state.add_entity("admin", "sensor.appdaemon_booted", utils.dt_to_str((await self.AD.sched.get_now()).replace(microsecond=0), self.AD.tz))
            warning_step = 0
            warning_iterations = 0

            # Start the loop proper

            thresh = 1000
            while not self.stopping:

                start_time = datetime.datetime.now().timestamp()

                try:

                    if self.AD.apps is True:

                        if self.AD.production_mode is False:
                            # Check to see if config has changed
                            s = datetime.datetime.now().timestamp()
                            await self.AD.app_management.check_app_updates()
                            e = datetime.datetime.now().timestamp()
                            if e - s > thresh:
                                self.logger.info("check_app_updates() took %s", e - s)

                    # Call me suspicious, but lets update state from the plugins periodically

                    s = datetime.datetime.now().timestamp()
                    await self.AD.plugins.update_plugin_state()
                    e = datetime.datetime.now().timestamp()
                    if e - s > thresh:
                        self.logger.info("update_plugin_state() took %s", e-s)


                    # Check for thread starvation

                    s = datetime.datetime.now().timestamp()
                    warning_step, warning_iterations = await self.AD.threading.check_q_size(warning_step, warning_iterations)
                    e = datetime.datetime.now().timestamp()
                    if e - s > thresh:
                        self.logger.info("check_q_size() took %s", e-s)

                    # Check for any overdue threads

                    s = datetime.datetime.now().timestamp()
                    await self.AD.threading.check_overdue_and_dead_threads()
                    e = datetime.datetime.now().timestamp()
                    if e - s > thresh:
                        self.logger.info("check_overdue_and_dead_threads() took %s", e-s)

                    # Save any hybrid namespaces

                    self.AD.state.save_hybrid_namespaces()

                    # Run utility for each plugin

                    self.AD.plugins.run_plugin_utility()

                    # Update uptime sensor

                    s = datetime.datetime.now().timestamp()
                    uptime = (await self.AD.sched.get_now()).replace(microsecond=0) - self.booted.replace(microsecond=0)
                    e = datetime.datetime.now().timestamp()
                    if e - s > thresh:
                        self.logger.info("get_now() took %s", e-s)

                    s = datetime.datetime.now().timestamp()
                    await self.AD.state.set_state("_utility", "admin", "sensor.appdaemon_uptime", state=str(uptime))
                    e = datetime.datetime.now().timestamp()
                    if e - s > thresh:
                        self.logger.info("set_state() took %s", e-s)

                except:
                    self.logger.warning('-' * 60)
                    self.logger.warning("Unexpected error during utility()")
                    self.logger.warning('-' * 60)
                    self.logger.warning(traceback.format_exc())
                    self.logger.warning('-' * 60)

                end_time = datetime.datetime.now().timestamp()

                loop_duration = (int((end_time - start_time) * 1000) / 1000) * 1000

                self.logger.debug("Util loop compute time: %sms", loop_duration)
                if self.AD.sched.realtime is True and loop_duration > (self.AD.max_utility_skew * 1000):
                    self.logger.warning("Excessive time spent in utility loop: %sms", loop_duration)
                    if self.AD.check_app_updates_profile is True:
                        self.logger.info("Profile information for Utility Loop")
                        self.logger.info(self.AD.app_management.check_app_updates_profile_stats)

                await asyncio.sleep(self.AD.utility_delay)

            if self.AD.app_management is not None:
                await self.AD.app_management.terminate()
