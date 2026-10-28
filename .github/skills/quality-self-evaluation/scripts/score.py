#!/usr/bin/env python3
"""Validate and summarize evidence-backed, per-rule self-evaluations."""

import argparse
import importlib.util
import json
import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


SCORE_STATUSES = {"score", "not_applicable", "unverified"}
ARTIFACT_KINDS = {"answer", "deliverable"}
COMPONENT_FIELDS = {
    "score": {"rule_id", "status", "score", "evidence"},
    "not_applicable": {"rule_id", "status", "reason"},
    "unverified": {"rule_id", "status", "reason"},
}
ARTIFACT_FIELDS = {"id", "kind", "occasions", "rules"}
INPUT_FIELDS = {"artifacts"}


class InputError(ValueError):
    pass


def project_root(value):
    if value:
        return Path(value).resolve()
    return Path(__file__).resolve().parents[4]


def load_rules(root):
    root = Path(root).resolve()
    entry = root / ".github" / "ocf" / "ocf.py"
    spec = importlib.util.spec_from_file_location("ocf_quality_policy", entry)
    if spec is None or spec.loader is None:
        raise InputError("cannot load the OCF policy engine at %s" % entry)
    ocf = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(ocf)
    except Exception as exc:
        raise InputError("cannot load the OCF policy engine: %s" % exc)
    policy, warnings = ocf.load_policy(str(root))
    if warnings:
        raise InputError("the OCF policy did not load cleanly: %s" % "; ".join(
            "[%s] %s" % item for item in warnings))
    findings = ocf.policy_findings(policy)
    if findings:
        raise InputError("the OCF policy has findings: %s" % "; ".join(
            "[%s] %s" % item for item in findings))
    rules = policy.get("rule", [])
    by_id = {}
    for rule in rules:
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise InputError("every policy rule must have a non-empty id")
        if rule_id in by_id:
            raise InputError("duplicate rule id in policy: %s" % rule_id)
        by_id[rule_id] = rule
    return ocf, policy, by_id


def exact_fields(value, expected, label):
    if not isinstance(value, dict):
        raise InputError("%s must be an object" % label)
    missing = expected - set(value)
    extra = set(value) - expected
    if missing or extra:
        details = []
        if missing:
            details.append("missing %s" % ", ".join(sorted(missing)))
        if extra:
            details.append("unknown %s" % ", ".join(sorted(extra)))
        raise InputError("%s: %s" % (label, "; ".join(details)))


def non_empty_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise InputError("%s must be a non-empty string" % label)
    return value.strip()


def rule_kind(rule):
    return "soft" if str(rule.get("kind", "hard")).strip().lower() == "soft" else "hard"


def effective_rule(ocf, rule, policy):
    return ocf.rule_switch(rule, policy)


def validate_rule_item(ocf, policy, rule, item, artifact, artifact_index):
    label = "artifact %r rule %r" % (artifact["id"], rule["id"])
    if not isinstance(item, dict):
        raise InputError("%s must be an object" % label)
    status = item.get("status")
    if not isinstance(status, str) or status not in SCORE_STATUSES:
        raise InputError("%s has an unknown status" % label)
    exact_fields(item, COMPONENT_FIELDS[status], label)

    if status == "score":
        score = item["score"]
        if type(score) is not int or score < 1 or score > 5:
            raise InputError("%s score must be an integer from 1 to 5" % label)
        evidence = non_empty_text(item["evidence"], label + " evidence")
    else:
        score = None
        evidence = None
        reason = non_empty_text(item["reason"], label + " reason")

    active = effective_rule(ocf, rule, policy)
    if not active and status != "not_applicable":
        raise InputError("%s is disabled in this policy and must be not_applicable" % label)

    if rule_kind(rule) == "soft":
        rule_occasions = set(ocf.comma_list((rule.get("if") or {}).get("occasion", "")))
        applies = bool(rule_occasions & set(artifact["occasions"]))
        if active and applies and status == "not_applicable":
            raise InputError("%s is active for this artifact; use score or unverified" % label)
        if active and not applies and status != "not_applicable":
            raise InputError("%s has no matching occasion and must be not_applicable" % label)

    if status == "score" and rule_kind(rule) == "hard":
        if not evidence:
            raise InputError("%s hard-rule score requires runtime evidence" % label)

    result = {"rule_id": rule["id"], "kind": rule_kind(rule), "status": status}
    if score is not None:
        result["score"] = score
        result["evidence"] = evidence
    else:
        result["reason"] = reason
    return result


def mean(scores):
    if not scores:
        return None
    return sum((Decimal(value) for value in scores), Decimal(0)) / Decimal(len(scores))


def display_number(value):
    if value is None:
        return None
    return str(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def hard_compliance(items):
    hard = [item for item in items if item["kind"] == "hard"]
    if not hard or all(item["status"] == "not_applicable" for item in hard):
        return "NOT_APPLICABLE"
    if any(item["status"] == "score" and item["score"] <= 2 for item in hard):
        return "FAIL"
    if any(item["status"] == "unverified" or
           (item["status"] == "score" and item["score"] == 3) for item in hard):
        return "UNVERIFIED"
    return "PASS"


def summarize_artifact(artifact, evaluated):
    numeric = [item["score"] for item in evaluated if item["status"] == "score"]
    unverified = sum(item["status"] == "unverified" for item in evaluated)
    not_applicable = sum(item["status"] == "not_applicable" for item in evaluated)
    return {
        "id": artifact["id"],
        "kind": artifact["kind"],
        "score": display_number(mean(numeric)),
        "scored_rules": len(numeric),
        "not_applicable_rules": not_applicable,
        "unverified_rules": unverified,
        "hard_compliance": hard_compliance(evaluated),
        "rules": evaluated,
    }, numeric


def evaluation_template(rules, answer_id="answer", deliverable_ids=()):
    def make_artifact(artifact_id, kind, occasions):
        return {
            "id": artifact_id,
            "kind": kind,
            "occasions": occasions,
            "rules": [{"rule_id": rule_id, "status": "unverified",
                       "reason": "Replace this placeholder with evidence or a genuine N/A reason."}
                      for rule_id in rules],
        }

    artifacts = [make_artifact(answer_id, "answer", ["answer"])]
    artifacts.extend(make_artifact(artifact_id, "deliverable", [])
                     for artifact_id in deliverable_ids)
    return {"artifacts": artifacts}


def evaluate(data, ocf, policy, rules):
    exact_fields(data, INPUT_FIELDS, "input")
    artifacts = data["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise InputError("artifacts must be a non-empty list")

    seen_ids = set()
    answer_artifacts = []
    deliverable_artifacts = []
    evaluated_artifacts = []
    answer_scores = []
    deliverable_scores = []
    all_hard = []

    for artifact_index, artifact in enumerate(artifacts):
        exact_fields(artifact, ARTIFACT_FIELDS, "artifact %d" % artifact_index)
        artifact_id = non_empty_text(artifact["id"], "artifact id")
        if artifact_id in seen_ids:
            raise InputError("duplicate artifact id: %s" % artifact_id)
        seen_ids.add(artifact_id)
        kind = artifact["kind"]
        if not isinstance(kind, str) or kind not in ARTIFACT_KINDS:
            raise InputError("artifact %r kind must be answer or deliverable" % artifact_id)
        occasions = artifact["occasions"]
        if not isinstance(occasions, list) or any(not isinstance(item, str) for item in occasions):
            raise InputError("artifact %r occasions must be a list of strings" % artifact_id)
        if len(set(occasions)) != len(occasions):
            raise InputError("artifact %r has duplicate occasions" % artifact_id)
        unknown_occasions = set(occasions) - set(ocf.SOFT_OCCASIONS)
        if unknown_occasions:
            raise InputError("artifact %r has unknown occasions: %s" %
                             (artifact_id, ", ".join(sorted(unknown_occasions))))
        if kind == "answer" and "answer" not in occasions:
            raise InputError("answer artifacts must include the answer occasion")
        if kind == "deliverable" and not occasions:
            raise InputError("deliverable artifacts must name a production occasion")
        artifact["id"] = artifact_id
        artifact["occasions"] = occasions

        entries = artifact["rules"]
        if not isinstance(entries, list):
            raise InputError("artifact %r rules must be a list" % artifact_id)
        by_rule = {}
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("rule_id"), str):
                raise InputError("artifact %r has a rule entry without a rule_id" % artifact_id)
            rule_id = entry["rule_id"]
            if rule_id not in rules:
                raise InputError("artifact %r has unknown rule id %r" % (artifact_id, rule_id))
            if rule_id in by_rule:
                raise InputError("artifact %r repeats rule id %r" % (artifact_id, rule_id))
            by_rule[rule_id] = entry
        missing = set(rules) - set(by_rule)
        if missing:
            raise InputError("artifact %r is missing rule ids: %s" %
                             (artifact_id, ", ".join(sorted(missing))))

        evaluated = [validate_rule_item(ocf, policy, rules[rule_id], by_rule[rule_id],
                                        artifact, artifact_index)
                     for rule_id in rules]
        summary, numeric = summarize_artifact(artifact, evaluated)
        evaluated_artifacts.append(summary)
        all_hard.extend(item for item in evaluated if item["kind"] == "hard")
        if kind == "answer":
            answer_artifacts.append(summary)
            answer_scores.extend(numeric)
        else:
            deliverable_artifacts.append(summary)
            deliverable_scores.extend(numeric)

    if len(answer_artifacts) != 1:
        raise InputError("input must contain exactly one answer artifact")
    # Aggregate the original integer ratings rather than rounded display values.
    answer_mean = mean(answer_scores)
    deliverables_mean = mean(deliverable_scores)
    if deliverable_artifacts:
        overall = mean([answer_mean, deliverables_mean] if answer_mean is not None and
                       deliverables_mean is not None else
                       ([answer_mean] if deliverables_mean is None and answer_mean is not None else
                        [deliverables_mean] if answer_mean is None and deliverables_mean is not None
                        else []))
    else:
        overall = answer_mean

    unverified_total = sum(item["unverified_rules"] for item in evaluated_artifacts)
    if overall is None:
        quality_status = "UNRATED"
    elif unverified_total:
        quality_status = "PARTIAL"
    else:
        quality_status = "COMPLETE"
    overall_hard = hard_compliance(all_hard)
    return {
        "quality_status": quality_status,
        "answer_mean": display_number(answer_mean),
        "deliverables_mean": display_number(deliverables_mean),
        "overall": display_number(overall),
        "hard_compliance": overall_hard,
        "unverified_rules": unverified_total,
        "artifacts": evaluated_artifacts,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="workspace root; defaults to this skill's project")
    parser.add_argument("--template", action="store_true",
                        help="print an unverified skeleton from the live policy rule IDs")
    parser.add_argument("--answer-id", default="answer", help="artifact ID for the final answer")
    parser.add_argument("--deliverable", action="append", default=[], metavar="ID",
                        help="add a deliverable artifact ID; may be repeated")
    args = parser.parse_args(argv)
    root = project_root(args.workspace)
    try:
        ocf, policy, rules = load_rules(root)
        if args.template:
            data = evaluation_template(rules, args.answer_id, args.deliverable)
            print(json.dumps(data, ensure_ascii=True, indent=2))
            return 0
        data = json.load(sys.stdin)
        result = evaluate(data, ocf, policy, rules)
    except (json.JSONDecodeError, InputError) as exc:
        print("score input error: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())