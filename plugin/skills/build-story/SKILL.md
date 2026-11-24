---
name: build-story
description: "Turns a refined hypothesis into a one-page pitch, an IDWeek abstract or a poster outline; for a case report, writes only the population-context paragraph."
---

<!-- Generated from src/health_data_registry/helpers.py by `make docs`. Do not edit by hand. -->

# Write my pitch or abstract

Help me build my story on: [topic, from my message]
(If my message doesn't say it, ask me for it in one short question first.)

1. Ask me which piece I need: a one-page pitch, an IDWeek abstract, a poster outline or a case report.
2. For a pitch, use these sections: the question; what the data shows (a small table); why it matters; what's already been published; the data I would use and its limits; next steps. Keep it to one page. Abstract and poster templates aren't written yet: for those, say so and draft the pitch only, which I can adapt.
3. Every number must carry its source, the date it was fetched, and its case definition; use get_provenance from this public health data tool for that. Mark anything provisional.
4. For a case report, write only the population-context paragraph and leave the case itself as "[your case, written outside this chat]". Never ask about the patient.
5. End with a citation list.

What we end with: the piece I asked for (a one-page pitch, an IDWeek abstract or a poster outline), with every number's source, date and case definition, and a citation list.

Rules for this whole conversation: cite every number to the tool it came from, with its source and date; never fill a number from memory or the news; show uncertainty ranges where GBD gives them and say that observed counts have none; flag provisional, suppressed or partial-geography numbers; and never ask me for, or accept, patient information.
