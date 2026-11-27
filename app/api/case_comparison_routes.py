import json
from dataclasses import asdict
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.api.routes import context, csrf, templates
from app.db.session import get_db
from app.models.db import Investigation
from app.services.case_comparison import MAX_CASES, compare_cases, comparison_csv

router = APIRouter()


def _selection(values: list[str]) -> list[str]:
    if not 2 <= len(values) <= MAX_CASES or len(set(values)) != len(values):
        raise HTTPException(422, f"Select between 2 and {MAX_CASES} different finished cases.")
    try:
        return [str(UUID(value)) for value in values]
    except ValueError as exc:
        raise HTTPException(422, "Invalid case selection.") from exc


@router.post("/compare-cases")
async def select_comparison(request: Request) -> Response:
    form = await request.form()
    csrf(request, str(form.get("csrf", "")))
    selected = _selection([str(value) for value in form.getlist("investigation_ids")])
    return RedirectResponse("/compare-cases?" + urlencode({"case": selected}, doseq=True), status_code=303)


@router.get("/compare-cases")
async def view_comparison(request: Request, db: Session = Depends(get_db)) -> Response:
    selected = _selection(request.query_params.getlist("case"))
    cases = list(db.scalars(select(Investigation).options(selectinload(Investigation.findings))
                           .where(Investigation.id.in_(selected))))
    if len(cases) != len(selected):
        raise HTTPException(404, "One or more selected cases no longer exist.")
    if any(case.status in {"queued", "running"} for case in cases):
        raise HTTPException(409, "Wait for the selected cases to finish before comparing them.")
    cases.sort(key=lambda case: selected.index(case.id))
    try:
        report = compare_cases(cases)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    format_name = request.query_params.get("format", "html")
    if format_name in {"json", "csv"}:
        payload = json.dumps(asdict(report), indent=2, ensure_ascii=False).encode() if format_name == "json" else comparison_csv(report)
        media_type = "application/json" if format_name == "json" else "text/csv"
        return Response(payload, media_type=media_type,
                        headers={"Content-Disposition": f'attachment; filename="corvus-case-comparison.{format_name}"'})
    if format_name != "html":
        raise HTTPException(422, "Supported comparison formats are html, json and csv.")
    return templates.TemplateResponse(request, "case_comparison.html", context(
        request, comparison=report, selection_query=urlencode({"case": selected}, doseq=True),
    ))
