"""Owner-only minimum/actual status presentation. No setting update or admission UI."""
from math import isfinite
from time import monotonic

from .live_loader import StatusObservation
from .metadata_http import valid_status_sample
from .newbuild_compatibility import BASE_MINIMUM_DVR_BUILD, version_admitted


def current(w):
    runtime = w._guide_runtime
    latest = runtime.latest_status
    if latest is None or w._live_session is None or w.transient_demo_mode is True:
        return None
    observation, context = latest
    if (type(observation) is not StatusObservation or not valid_status_sample(observation.sample)
            or not runtime._same_context(context) or w._development_blocked is not None
            or not w._metadata_loader.current_status_receipt(observation.receipt)
            or runtime._request is not None or runtime._mode is not None
            or w._live_session.loading or w._closed):
        return None
    try:
        if (context.saved_connect is not True
                or type(context.saved_developer) is not bool
                or w.get_setting('development_metadata_enabled') is not context.saved_connect
                or w.get_setting('ShowDeveloperTools') is not context.saved_developer
                or w._saved_config() != observation.config
                or w._development_config != observation.config):
            return None
    except Exception:
        return None
    return latest


def fresh(sample):
    age = monotonic() - sample.observed_at
    return isfinite(sample.observed_at) and sample.observed_at >= 0 and isfinite(age) and 0 <= age < 60


def status_text(w):
    latest = current(w)
    reported = latest[0].sample.reported_version if latest else w.t('unavailable')
    passed = latest is not None and version_admitted(BASE_MINIMUM_DVR_BUILD, reported)
    body = w.t('minimum_build_status', minimum=BASE_MINIMUM_DVR_BUILD,
               reported=reported, verified=w.t('yes' if passed else 'no'))
    if latest is None:
        body += '\n' + w.t('minimum_build_pending')
    elif not fresh(latest[0].sample):
        body += '\n' + w.t('minimum_build_observation_expired')
    elif not passed:
        body += '\n' + w.t('minimum_build_failed')
    return body + '\n\n' + w.t('minimum_build_policy')
