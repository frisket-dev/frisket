"""Save disagreement answers into ordinary, visible, project-local sheets."""

from frisket.authoring import column_types
from frisket.contracts.http.evaluation import ExpectedAnswerRequest
from frisket.engine.store import evaluation as store
from frisket.engine.store.artifact_timeline import TimelineError
from frisket.features.temporal_ingress import preflight_typed_rows_for_persistence
from frisket.server.route_errors import RouteError
from frisket.server.services import evaluation_links as links
from frisket.server.services.evaluation_values import full_column


def save_expected_answer(service, pid, raw):
    body = ExpectedAnswerRequest.model_validate(raw).model_dump(mode="json")
    if body["value"] == "":
        body["value"] = None
    project = service.workspace.get(pid)
    context = service._context(pid, body["basis"], [body["row_id"]])
    sheet_id = context["sheet"]["id"]
    if body["row_id"] not in project.visible_row_ids(sheet_id, [body["row_id"]]):
        raise RouteError(422, "Expected answer row belongs to another sheet")
    output = next(
        (
            c
            for c in context["output_columns"]
            if c["logical_key"] == body["logical_key"]
        ),
        None,
    )
    if output is None or output["column_id"] is None:
        raise RouteError(
            422,
            "Run the action to create its output columns before joining expected answers",
        )
    current_outputs = {
        column["logical_key"]: column["column_id"]
        for column in context["output_columns"]
    }
    link = store.link(project, body["link_id"]) if body["link_id"] is not None else None
    if body["link_id"] is not None and link is None:
        raise RouteError(404, "Expected answer link was not found")
    if link is None:
        link = next(
            (
                item
                for item in store.links(project, sheet_id)
                if item["action_id"] == context["draft"]["action_id"]
                and any(
                    field["logical_key"] == body["logical_key"]
                    and field["output_column_id"] == output["column_id"]
                    for field in item["fields"]
                )
                and all(
                    current_outputs.get(field["logical_key"])
                    == field["output_column_id"]
                    for field in item["fields"]
                )
            ),
            None,
        )
    if link is not None and (
        link["target_sheet_id"] != sheet_id
        or link["action_id"] != context["draft"]["action_id"]
    ):
        raise RouteError(409, "Expected link belongs to another action or sheet")
    if link is not None and any(
        current_outputs.get(field["logical_key"]) != field["output_column_id"]
        for field in link["fields"]
    ):
        raise RouteError(
            409, "Expected answer link belongs to another output column family"
        )
    if link is None and body["value"] in (None, ""):
        raise RouteError(422, "Save a nonblank expected answer first")
    with store.write_transaction(project):
        if link is not None:
            links.link_payload(project, link)
        if link is None:
            name = f"{context['sheet']['name']} · expected"
            existing_names = {row["name"] for row in project.sheets()}
            base, index = name, 2
            while name in existing_names:
                name = f"{base} {index}"
                index += 1
            expected_sheet = project.add_sheet(name, commit=False)
            row_key = project.add_column(
                expected_sheet,
                "_target_row_id",
                "integer",
                default_hidden=True,
                commit=False,
            )
            expected_column = project.add_column(
                expected_sheet, output["current_name"], commit=False
            )
            link = {
                "target_sheet_id": sheet_id,
                "expected_sheet_id": expected_sheet,
                "action_id": context["draft"]["action_id"],
                "keys": [{"expected_column_id": row_key, "target_column_id": None}],
                "fields": [
                    {
                        "expected_column_id": expected_column,
                        "output_column_id": output["column_id"],
                        "logical_key": body["logical_key"],
                        "rule": body["rule"],
                        "hidden": False,
                        "source": "disagreements",
                    }
                ],
                "hidden": False,
            }
            link["id"] = store.save_link(project, link)
        field = next(
            (f for f in link["fields"] if f["logical_key"] == body["logical_key"]),
            None,
        )
        if field is None:
            field = {
                "expected_column_id": project.add_column(
                    link["expected_sheet_id"], output["current_name"], commit=False
                ),
                "output_column_id": output["column_id"],
                "logical_key": body["logical_key"],
                "rule": body["rule"],
                "hidden": False,
                "source": "disagreements",
            }
            link["fields"].append(field)
            store.save_link(project, link, link["id"])
        mapping, preview = links.row_matches(project, link)
        links.require_unambiguous(preview)
        source_row = mapping.get(body["row_id"])
        if source_row is None:
            record = {}
            column_ids = {}
            for pair in link["keys"]:
                ec = links.column(
                    project, pair["expected_column_id"], link["expected_sheet_id"]
                )
                if pair["target_column_id"] is None:
                    value = body["row_id"]
                else:
                    read = full_column(
                        project,
                        sheet_id,
                        pair["target_column_id"],
                        [body["row_id"]],
                    ).get(body["row_id"])
                    value = (
                        read.value if read is not None and read.value_present else None
                    )
                    if value is None:
                        raise RouteError(
                            422,
                            "Target row needs a complete join key before adding expected answers",
                        )
                record[ec["name"]], column_ids[ec["name"]] = value, ec["id"]
            expected_column = links.column(
                project, field["expected_column_id"], link["expected_sheet_id"]
            )
            record[expected_column["name"]], column_ids[expected_column["name"]] = (
                body["value"],
                expected_column["id"],
            )
            types = {
                name: str(project.get_column(column_id)["type"])
                for name, column_id in column_ids.items()
            }
            for name, value in record.items():
                try:
                    parsed = column_types.parse_value(types[name], value)
                except Exception as exc:
                    raise RouteError(
                        422,
                        f"Expected value for column {name!r} could not be parsed as {types[name]}",
                    ) from exc
                if not column_types.validate_value(types[name], parsed):
                    hint = (
                        "; use a text expected column for [empty]"
                        if value == "[empty]"
                        else ""
                    )
                    raise RouteError(
                        422,
                        f"Expected value for column {name!r} must match its {types[name]} type{hint}",
                    )
                record[name] = parsed
            try:
                record = preflight_typed_rows_for_persistence(
                    project, column_types_by_name=types, rows=[record]
                )[0]
            except TimelineError as exc:
                raise RouteError(
                    422, f"Expected row has an invalid temporal value: {exc.message}"
                ) from exc
            source_row = project.add_row_with_undo(
                link["expected_sheet_id"], record, column_ids
            )
            # A target key can become ambiguous since the last edit.
            links.require_unambiguous(links.row_matches(project, link)[1])
            inserted = True
        else:
            inserted = False
    if not inserted:
        service._edit_cell(
            pid, project, source_row, field["expected_column_id"], body["value"]
        )
    return links.link_payload(project, store.link(project, link["id"]))
