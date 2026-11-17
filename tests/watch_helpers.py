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


def add_watch(project, name, *, query, scope="sheet", sheet_id=None, **kwargs):
    """Author existing scenario fixtures through the unified View/alert API."""
    from frisket.engine.views.specs import legacy_selection

    scope_def = {"kind": scope}
    if sheet_id is not None:
        scope_def["sheet_id"] = sheet_id
    selection = legacy_selection(
        project,
        sheet_id=sheet_id,
        spec={**query, "scope": query.get("scope", scope_def)},
    )
    view_id = project.add_view(name, {"selection": selection, "presentation": {}})
    return project.add_watch(name, view_id=view_id, **kwargs)


def post_watch(client, path, *, json):
    """Build a saved View for an existing alert scenario, then call the real API."""
    from frisket.engine.views.specs import legacy_selection

    pid = path.split("/")[3]
    project = client.app.state.workspace.get(pid)
    body = dict(json)
    query = body.pop("query")
    scope = body.pop(
        "scope",
        query.get("scope")
        or (
            {"kind": "sheet", "sheet_id": query["sheet_id"]}
            if query.get("sheet_id") is not None
            else {"kind": "project"}
        ),
    )
    selection = legacy_selection(
        project, sheet_id=scope.get("sheet_id"), spec={**query, "scope": scope}
    )
    view = client.post(
        f"/api/projects/{pid}/views",
        json={"name": body["name"], "selection": selection},
    )
    if view.status_code != 200:
        return view
    return client.post(path, json={**body, "view_id": view.json()["id"]})
