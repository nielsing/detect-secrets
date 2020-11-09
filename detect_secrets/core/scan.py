from functools import lru_cache
from importlib import import_module
from typing import Generator
from typing import IO
from typing import List
from typing import Optional
from typing import Tuple

from . import plugins
from ..settings import get_settings
from ..transformers import get_transformers
from ..transformers import ParsingError
from ..types import SelfAwareCallable
from ..util.inject import get_injectable_variables
from ..util.inject import inject_variables_into_function
from .log import log
from .plugins import Plugin
from .potential_secret import PotentialSecret


def scan_line(line: str) -> Generator[PotentialSecret, None, None]:
    """Used for adhoc string scanning."""
    # Disable this, since it doesn't make sense to run this for adhoc usage.
    get_settings().disable_filters(
        'detect_secrets.filters.common.is_invalid_file',
    )

    for plugin in get_plugins():
        yield from _scan_line(
            plugin=plugin,
            filename='adhoc-string-scan',
            line=line,
            line_number=0,
        )


def scan_file(filename: str) -> Generator[PotentialSecret, None, None]:
    if not get_plugins():   # pragma: no cover
        log.warning('No plugins to scan with!')
        return

    if _filter_files(filename):
        return

    try:
        with open(filename) as f:
            log.info(f'Checking file: {filename}')

            lines = _get_transformed_file(f)
            if not lines:
                lines = f.readlines()

            has_secret = False
            for secret in _process_line_based_plugins(
                lines=list(enumerate(lines, 1)),
                filename=f.name,
            ):
                has_secret = True
                yield secret

            if has_secret:
                return

            # Only if no secrets, then use eager transformers
            f.seek(0)
            lines = _get_transformed_file(f, use_eager_transformers=True)
            if not lines:
                return

            yield from _process_line_based_plugins(
                lines=list(enumerate(lines, 1)),
                filename=f.name,
            )
    except IOError:
        log.warning(f'Unable to open file: {filename}')


def scan_diff(diff: str) -> Generator[PotentialSecret, None, None]:
    """
    :raises: ImportError
    """
    # Local imports, so that we don't need to require unidiff for versions of
    # detect-secrets that don't use it.
    from unidiff import PatchSet

    if not get_plugins():   # pragma: no cover
        log.warning('No plugins to scan with!')
        return

    patch_set = PatchSet.from_string(diff)
    for patch_file in patch_set:
        filename = patch_file.path
        if _filter_files(filename):
            continue

        lines = [
            (line.target_line_no, line.value)
            for chunk in patch_file
            # target_lines refers to incoming (new) changes
            for line in chunk.target_lines()
            if line.is_added
        ]

        yield from _process_line_based_plugins(lines, filename=filename)


def _filter_files(filename: str) -> bool:
    """Returns True if successfully filtered."""
    for filter_fn in get_filters():
        if inject_variables_into_function(filter_fn, filename=filename):
            log.info(f'Skipping "{filename}" due to "{filter_fn.path}"')
            return True

    return False


def _get_transformed_file(file: IO, use_eager_transformers: bool = False) -> Optional[List[str]]:
    for transformer in get_transformers():
        if not transformer.should_parse_file(file.name):
            continue

        if use_eager_transformers != transformer.is_eager:
            continue

        try:
            return transformer.parse_file(file)
        except ParsingError:
            pass
        finally:
            file.seek(0)

    return None


def _process_line_based_plugins(
    lines: List[Tuple[int, str]],
    filename: str,
) -> Generator[PotentialSecret, None, None]:
    # NOTE: We iterate through lines *then* plugins, because we want to quit early if any of the
    # filters return True.
    for line_number, line in lines:
        line = line.rstrip()

        # We apply line-specific filters, and see whether that allows us to quit early.
        if any([
            inject_variables_into_function(filter_fn, filename=filename, line=line)
            for filter_fn in get_filters_with_parameter('line')
        ]):
            continue

        for plugin in get_plugins():
            yield from _scan_line(plugin, filename, line, line_number)


def _scan_line(
    plugin: Plugin,
    filename: str,
    line: str,
    line_number: int,
) -> Generator[PotentialSecret, None, None]:
    # NOTE: We don't apply filter functions here yet, because we don't have any filters
    # that operate on (filename, line, plugin) without `secret`
    try:
        secrets = plugin.analyze_line(filename=filename, line=line, line_number=line_number)
    except AttributeError:
        return

    if not secrets:
        return

    for secret in secrets:
        for filter_fn in get_filters_with_parameter('secret'):
            if inject_variables_into_function(
                filter_fn,
                filename=secret.filename,
                secret=secret.secret_value,
                plugin=plugin,
                line=line,
            ):
                log.debug(f'Skipping "{secret.secret_value}" due to `{filter_fn.path}`.')
                break
        else:
            yield secret


@lru_cache(maxsize=1)
def get_plugins() -> List[Plugin]:
    return [
        plugins.initialize.from_plugin_classname(classname)
        for classname in get_settings().plugins
    ]


def get_filters_with_parameter(*parameters: str) -> List[SelfAwareCallable]:
    """
    The issue of our method of dependency injection is that functions will be called multiple
    times. For example, if we have two functions:

    >>> def foo(filename: str): ...
    >>> def bar(filename: str, secret: str): ...

    our invocation of `inject_variables_into_function(filename=filename, secret=secret)`
    will run both of these functions. While expected, this results in multiple invocations of
    the same function, which can be less than ideal (especially if we have a heavy duty filter).

    To address this, we filter our filters with this function. It will return the functions
    that accept a minimum set of parameters, to avoid duplicative work. For instance,

    >>> get_filters_with_parameter('secret')
    [bar]
    """
    minimum_parameters = set(parameters)

    return [
        filter
        for filter in get_filters()
        if minimum_parameters <= filter.injectable_variables
    ]


@lru_cache(maxsize=1)
def get_filters() -> List[SelfAwareCallable]:
    output = []
    for path, config in get_settings().filters.items():
        module_path, function_name = path.rsplit('.', 1)
        try:
            function = getattr(import_module(module_path), function_name)
        except (ModuleNotFoundError, AttributeError):
            log.warning(f'Invalid filter: {path}')
            continue

        # We attach this metadata to the function itself, so that we don't need to
        # compute it everytime. This will allow for dependency injection for filters.
        function.injectable_variables = set(get_injectable_variables(function))
        output.append(function)

        # This is for better logging.
        function.path = path

    return output