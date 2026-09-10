"""A closed frame queue must not discard failed process-tree teardown."""

import asyncio
import json

import pytest

from frisket.engine.sandbox.shim import SandboxTeardownError
from frisket.plugins.process_client import PluginProcessClient
from tests.plugins.test_plugin_process_client import _importer_request


def failed_teardown_runner(failure, *, prefix, row_count=1):
    async def runner(_argv, *, on_stdout_line, should_cancel, **kwargs):
        on_stdout_line(
            json.dumps(
                {"type": "schema", "columns": [{"name": "value", "type": "integer"}]}
            )
        )
        for number in range(row_count):
            on_stdout_line(json.dumps({"type": "row", "row": {"value": number}}))
        if prefix:
            while not should_cancel():
                await asyncio.sleep(0)
        else:
            on_stdout_line(json.dumps({"type": "done", "row_count": row_count}))
        raise failure

    return runner


@pytest.mark.parametrize("prefix", [True, False])
def test_importer_retains_fatal_teardown_on_prefix_close_and_full_stream(prefix):
    failure = SandboxTeardownError("child death was not proved")
    runner = failed_teardown_runner(failure, prefix=prefix)

    client = PluginProcessClient("unused", run_sandboxed_stdout_lines_call=runner)
    frames = client.importer(_importer_request(), {})
    with pytest.raises(SandboxTeardownError) as error:
        if prefix:
            assert next(frames).type == "schema"
            assert next(frames).type == "row"
            frames.close()
        else:
            list(frames)
    assert error.value is failure
