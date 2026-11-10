"""Unified tools package for the RPD Transformation Engine (app/tools/__init__.py)."""

from typing import Any

from .matcher import (
    DEFAULT_DB_PATH,
    DEVELOPMENT_OR_EXCLUDED_KEYWORDS,
    INVALID_REF_VALUES,
    VALID_DEPLOYED_STATUSES,
    MatchUseCasesInput,
    RankedUseCasePool,
    UseCaseCandidate,
    _connect_readonly_sqlite,
    _extract_stem_variants,
    is_deployed_and_not_in_development,
    load_repository_from_sqlite,
    match_and_score_use_cases,
    normalize_repository_columns,
)


def __getattr__(name: str) -> Any:
    if name in (
        "CustomerContextSchema",
        "IngestSourcesInput",
        "IngestSourcesOutput",
        "_is_safe_public_url",
        "ingest_customer_sources",
    ):
        from . import discovery

        return getattr(discovery, name)
    if name in (
        "ApprovedUseCases",
        "present_candidates_for_review",
        "submit_approved_use_cases",
    ):
        from . import hitl

        return getattr(hitl, name)
    if name in (
        "ExtractValueInput",
        "VerifiedKpiRecord",
        "UseCaseValueAnalysis",
        "ValueAnalysisDossier",
        "extract_verified_value_and_kpis",
    ):
        from . import value_tools

        return getattr(value_tools, name)
    if name in (
        "KpiAchievedItem",
        "TransformationReportItem",
        "TransformationOpportunityReport",
        "synthesize_transformation_report",
    ):
        from . import synthesizer

        return getattr(synthesizer, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CustomerContextSchema",
    "IngestSourcesInput",
    "IngestSourcesOutput",
    "_is_safe_public_url",
    "ingest_customer_sources",
    "DEFAULT_DB_PATH",
    "DEVELOPMENT_OR_EXCLUDED_KEYWORDS",
    "INVALID_REF_VALUES",
    "VALID_DEPLOYED_STATUSES",
    "MatchUseCasesInput",
    "UseCaseCandidate",
    "RankedUseCasePool",
    "_connect_readonly_sqlite",
    "_extract_stem_variants",
    "is_deployed_and_not_in_development",
    "load_repository_from_sqlite",
    "normalize_repository_columns",
    "match_and_score_use_cases",
    "ApprovedUseCases",
    "present_candidates_for_review",
    "submit_approved_use_cases",
    "ExtractValueInput",
    "VerifiedKpiRecord",
    "UseCaseValueAnalysis",
    "ValueAnalysisDossier",
    "extract_verified_value_and_kpis",
    "KpiAchievedItem",
    "TransformationReportItem",
    "TransformationOpportunityReport",
    "synthesize_transformation_report",
]
