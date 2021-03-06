import json
import logging

from enum import Enum
from typing import Dict
from typing import Optional
from typing import Sequence
from typing import Set

from baseplate import Span
from baseplate.clients import ContextFactory
from baseplate.lib import config
from baseplate.lib.edge_context import User
from baseplate.lib.events import DebugLogger
from baseplate.lib.events import EventLogger
from baseplate.lib.experiments.providers import parse_experiment
from baseplate.lib.experiments.providers.base import Experiment
from baseplate.lib.file_watcher import FileWatcher
from baseplate.lib.file_watcher import WatchedFileNotAvailableError


logger = logging.getLogger(__name__)


class EventType(Enum):
    EXPOSE = "expose"
    BUCKET = "choose"


class ExperimentsContextFactory(ContextFactory):
    """Experiment client context factory.

    This factory will attach a new
    :py:class:`baseplate.lib.experiments.Experiments` to an attribute on the
    :py:class:`~baseplate.RequestContext`.

    :param path: Path to the experiment configuration file.
    :param event_logger: The logger to use to log experiment eligibility
        events. If not provided, a :py:class:`~baseplate.lib.events.DebugLogger`
        will be created and used.
    :param timeout: How long, in seconds, to block instantiation waiting
        for the watched experiments file to become available (defaults to not
        blocking).
    """

    def __init__(
        self, path: str, event_logger: Optional[EventLogger] = None, timeout: Optional[float] = None
    ):
        self._filewatcher = FileWatcher(path, json.load, timeout=timeout)
        self._event_logger = event_logger

    def make_object_for_context(self, name: str, span: Span) -> "Experiments":
        return Experiments(
            config_watcher=self._filewatcher,
            server_span=span,
            context_name=name,
            event_logger=self._event_logger,
        )


class Experiments:
    """Access to experiments with automatic refresh when changed.

    This experiments client allows access to the experiments cached on disk by
    the experiment configuration fetcher daemon.  It will automatically reload
    the cache when changed.  This client also handles logging bucketing events
    to the event pipeline when it is determined that the request is part of an
    active variant.
    """

    def __init__(
        self,
        config_watcher: FileWatcher,
        server_span: Span,
        context_name: str,
        event_logger: Optional[EventLogger] = None,
    ):
        self._config_watcher = config_watcher
        self._span = server_span
        self._context_name = context_name
        self._already_bucketed: Set[str] = set()
        self._experiment_cache: Dict[str, Optional[Experiment]] = {}
        if event_logger:
            self._event_logger = event_logger
        else:
            self._event_logger = DebugLogger()

    def _get_config(self, name: str) -> Optional[Dict[str, str]]:
        try:
            config_data = self._config_watcher.get_data()
            return config_data[name]
        except WatchedFileNotAvailableError as exc:
            logger.warning("Experiment config unavailable: %s", str(exc))
        except KeyError:
            logger.warning("Experiment <%r> not found in experiment config", name)
        except TypeError as exc:
            logger.warning("Could not load experiment config: %s", str(exc))
        return None

    def _get_experiment(self, name: str) -> Optional[Experiment]:
        if name not in self._experiment_cache:
            experiment_config = self._get_config(name)
            if not experiment_config:
                experiment = None
            else:
                try:
                    experiment = parse_experiment(experiment_config)
                except Exception as err:
                    logger.error("Invalid configuration for experiment %s: %s", name, err)
                    return None
            self._experiment_cache[name] = experiment
        return self._experiment_cache[name]

    def get_all_experiment_names(self) -> Sequence[str]:
        """Return a list of all valid experiment names from the configuration file.

        :return: List of all valid experiment names.
        """
        cfg = self._config_watcher.get_data()
        experiment_names = list(cfg.keys())
        return experiment_names

    def is_valid_experiment(self, name: str) -> bool:
        """Return true if the provided experiment name is a valid experiment.

        :param name: Name of the experiment you want to check.

        :return: Whether or not a particular experiment is valid.
        """
        return self._get_experiment(name) is not None

    def variant(
        self,
        name: str,
        user: Optional[User] = None,
        bucketing_event_override: Optional[bool] = None,
        **kwargs: str,
    ) -> Optional[str]:
        r"""Return which variant, if any, is active.

        If a variant is active, a bucketing event will be logged to the event
        pipeline unless any one of the following conditions are met:

        1. bucketing_event_override is set to False.
        2. The experiment specified by "name" explicitly disables bucketing
           events.
        3. We have already logged a bucketing event for the value specified by
           ``experiment.get_unique_id(\*\*kwargs)`` within the current
           request.

        Since checking the status an experiment will fire a bucketing event, it
        is best to only check the variant when you are making the decision that
        will expose the experiment to the user.  If you absolutely must check
        the status of an experiment before you are sure that the experiment
        will be exposed to the user, you can use `bucketing_event_override` to
        disabled bucketing events for that check.

        :param name: Name of the experiment you want to run.
        :param user: User object for the user you want to check the experiment
            variant for.  If you set user, the experiment parameters for that user
            ("user_id", "logged_in", and "user_roles") will be extracted and added
            to the inputs to the call to Experiment.variant.  The user's
            event_fields will also be extracted and added to the bucketing event if
            one is  logged.  It is recommended that you provide a value for user
            rather than setting the user parameters manually in ``kwargs``.
        :param bucketing_event_override: Set if you need to override the
            default behavior for sending bucketing events.  This parameter should
            be set sparingly as it breaks the assumption that you will fire a
            bucketing event when you first check the state of an experiment.  If
            set to False, will never send a bucketing event.  If set to None, no
            override will be applied.  Set to None by default.  Note that setting
            bucketing_event_override to True has no effect, it will behave the same
            as when it is set to None.
        :param kwargs:  Arguments that will be passed to experiment.variant to
            determine bucketing, targeting, and overrides. These values will also
            be passed to the logger.

        :return: Variant name if a variant is active, None otherwise.
        """
        experiment = self._get_experiment(name)

        if experiment is None:
            return None

        inputs = dict(kwargs)

        if user:
            inputs.update(user.event_fields())

        variant = experiment.variant(**inputs)

        bucketing_id = experiment.get_unique_id(**inputs)

        do_log = True

        if not bucketing_id:
            do_log = False

        if variant is None:
            do_log = False

        if bucketing_event_override is False:
            do_log = False

        if bucketing_id and bucketing_id in self._already_bucketed:
            do_log = False

        do_log = do_log and experiment.should_log_bucketing()

        if do_log:
            assert bucketing_id
            self._event_logger.log(
                experiment=experiment,
                variant=variant,
                user_id=inputs.get("user_id"),
                logged_in=inputs.get("logged_in"),
                cookie_created_timestamp=inputs.get("cookie_created_timestamp"),
                app_name=inputs.get("app_name"),
                event_type=EventType.BUCKET,
                inputs=inputs,
                span=self._span,
            )
            self._already_bucketed.add(bucketing_id)

        return variant

    def expose(
        self, experiment_name: str, variant_name: str, user: Optional[User] = None, **kwargs: str
    ) -> None:
        """Log an event to indicate that a user has been exposed to an experimental treatment.

        :param experiment_name: Name of the experiment that was exposed.
        :param variant_name: Name of the variant that was exposed.
        :param user: User object for the user you want to check the experiment
            variant for. If unset, it is expected that user_id and logged_in values
            will be set in the keyword arguments.
        :param kwargs: Additional arguments that will be passed to logger.

        """
        experiment = self._get_experiment(experiment_name)

        if experiment is None:
            return

        inputs = dict(kwargs)

        if user:
            inputs.update(user.event_fields())

        self._event_logger.log(
            experiment=experiment,
            variant=variant_name,
            user_id=inputs.get("user_id"),
            logged_in=inputs.get("logged_in"),
            cookie_created_timestamp=inputs.get("cookie_created_timestamp"),
            app_name=inputs.get("app_name"),
            event_type=EventType.EXPOSE,
            inputs=inputs,
            span=self._span,
        )


def experiments_client_from_config(
    app_config: config.RawConfig, event_logger: EventLogger, prefix: str = "experiments."
) -> ExperimentsContextFactory:
    """Configure and return an :py:class:`ExperimentsContextFactory` object.

    The keys useful to :py:func:`experiments_client_from_config` should be prefixed, e.g.
    ``experiments.path``, etc.

    Supported keys:

    ``path``: the path to the experiment configuration file generated by the
        experiment configuration fetcher daemon.
    ``timeout`` (optional): the time that we should wait for the file specified by
        ``path`` to exist.  Defaults to `None` which is `infinite`.

    :param raw_config: The application configuration which should have
        settings for the experiments client.
    :param event_logger: The EventLogger to be used to log bucketing events.
    :param prefix: the prefix used to filter keys (defaults to "experiments.").

    """
    assert prefix.endswith(".")
    config_prefix = prefix[:-1]

    cfg = config.parse_config(
        app_config,
        {
            config_prefix: {
                "path": config.Optional(config.String, default="/var/local/experiments.json"),
                "timeout": config.Optional(config.Timespan),
            }
        },
    )
    options = getattr(cfg, config_prefix)

    # pylint: disable=maybe-no-member
    if options.timeout:
        timeout = options.timeout.total_seconds()
    else:
        timeout = None

    return ExperimentsContextFactory(options.path, event_logger, timeout=timeout)
