"""Alert fixtures with an explicit initial successful observation."""

from frisket.features.watchlists.service import resolve_watch_run


def seed_empty_watch_baseline(project, watch_id):
    resolution = resolve_watch_run(project, project.get_watch(watch_id))
    return project.record_watch_run(
        watch_id,
        hits=[],
        op_cursor_before=project.op_cursor,
        op_cursor_after=project.op_cursor,
        resolved_query_hash=resolution["resolved_query_hash"],
        resolved_query=resolution["resolved_query"],
    )
