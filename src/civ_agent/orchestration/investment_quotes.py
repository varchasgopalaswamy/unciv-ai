"""Bind copied quote scalars to already delivered native observation versions."""

from civ_agent.orchestration.contracts import canonical
from civ_agent.protocol import JsonObject


def quote_sources(snapshot) -> list[JsonObject]:
    selected: list[JsonObject] = []
    for key, record in snapshot.get("records", {}).items():
        if record.get("category") != "observation" or snapshot.get("delivered", {}).get(key) != record.get("version"):
            continue
        body = record.get("body")
        if not isinstance(body, dict):
            continue
        facts = []

        def visit(value, path="", depth=0, collected=facts):
            if len(collected) >= 20 or depth > 10:
                return
            if isinstance(value, dict):
                for name, child in value.items():
                    visit(child, path + "/" + name.replace("~", "~0").replace("/", "~1"), depth + 1)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    visit(child, path + "/" + str(index), depth + 1)
            elif any(word in path.casefold() for word in ("cost", "price", "turns", "purchase", "production")):
                if len(path) <= 160 and (not isinstance(value, str) or len(value) <= 400):
                    collected.append({"field": path, "value": value})

        visit(body)
        if facts:
            selected.append(
                {"source": key, "version": record["version"], "observedTurn": body.get("turn"), "facts": facts}
            )
        if len(selected) >= 16:
            break
    return selected


def quote_error(choice, snapshot):
    sources = {row["source"]: row for row in quote_sources(snapshot)}
    for option in choice.comparison:
        quote = option.quote
        if quote is None or quote.availability != "observed":
            continue
        delivered = sources.get(quote.source)
        if delivered is None:
            return "The quoted native source was not delivered to this job. Copy source, version and facts from review_investments().progress.quoteSources after the relevant native inspection, or record unknown. Detail evidence labels and queue revisions are different identifiers."
        if quote.version != delivered["version"]:
            return "The quote source version changed; refresh or record uncertainty."
        if quote.observedTurn is not None and quote.observedTurn != delivered["observedTurn"]:
            return "The quote's observed turn differs from its delivered native source; refresh or record uncertainty."
        fields = {item["field"]: item["value"] for item in delivered["facts"]}
        for fact in quote.facts:
            if fact.field not in fields or canonical(fact.value) != canonical(fields[fact.field]):
                return "A quote field differs from its delivered native source. Copy quoteSources exactly; no strategy edit was applied."
    return None
