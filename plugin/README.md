# Health Data Registry plugin

<!-- Generated from src/health_data_registry/helpers.py by `make docs`. Do not edit by hand. -->

Adds the health-data-registry connector and its research workflows to Claude. The connector answers plain questions about infectious disease with observed, population-level public data (CDC, WHO), each number with its source, version and method. Use it beside your own IHME GBD and PubMed connectors.

## Use it

Add the plugin, connect the connector from the plugin's Connectors tab, then start with `start-here`. The skills:

- `start-here`: Tests each connected research tool and says in plain words which work and which are missing. Use first, in a new chat or project.
- `research-story`: The entry point for a research project: asks whether you already have an area or want one found, then routes you through scoping or finding opportunities, refining a hypothesis and building the story.
- `scope-an-area`: Path A: takes an area or question in your own words and returns the datasets that fit, what each can't answer, and the questions worth refining.
- `find-opportunities`: Path B: for a fellow without an area yet, looks for signals in the public data worth a study and returns a short opportunity brief.
- `refine-hypothesis`: The refining loop: states the hypothesis, checks the data and whether it's new, names the confounders and what the data can't say, and revises until it's test-ready, needs revising, or should be dropped.
- `build-story`: Turns a refined hypothesis into a one-page pitch, an IDWeek abstract or a poster outline; for a case report, writes only the population-context paragraph.
- `choose-format`: Asks a few short questions about your project and returns which output fits now (conference abstract or poster, research letter or brief report, full original manuscript, or viewpoint or perspective), how settled that is, and what would change it.
- `help-me-fix-it`: Asks what you see, matches it to known problems, and gives the fix one step at a time.

## Data

The connector is read-only and reads only catalogued public sources. It sends your questions to the hosted server and stores nothing about you. Never share patient information with it.
