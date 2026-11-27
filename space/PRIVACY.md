# Privacy

This connector is for doctors asking about public, population-level data. It has no sign-in, no accounts and no
analytics.

**What this server keeps about you: nothing.** It never writes down what you asked, the words Claude sent to a
tool, or which places, years or conditions you looked up. Its log holds only counts: how many times each tool was
used, whether it answered or refused, and roughly how long it took (for example "get_series, answered, under 1
second: 12"). It doesn't log addresses or connection identifiers.

**What it stores:** copies of public data files from the project's open data hub, so it doesn't download them
again for every question. Data from WHO is read from WHO when asked, held in memory for a few minutes, and never
written to disk.

**Never send patient information.** The tools take questions about populations: a condition, places and years.
Don't put anything about a patient into a question.

**Who else is involved:** Claude (run by Anthropic) decides when to call a tool and sends the request; Hugging
Face runs this Space and passes each request to it. Hugging Face records some information about every visit,
such as the IP address (see its [privacy policy](https://huggingface.co/privacy)); for requests from Claude that
address is Anthropic's, not yours. This server receives only the tool requests Claude sends, not your
conversation.

Questions or concerns: open an issue at <https://github.com/jtouley/health-data-registry/issues>.
