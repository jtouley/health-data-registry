# Setting up your research tools

This page gets you from nothing to a working chat. It covers this tool (health-data-registry) and the two
connectors it works best with: IHME GBD and PubMed.

A **connector** is an add-on that lets Claude use an outside tool. You add each one once. An **MCP server** is the
technical name for the program behind a connector; you don't need to know more than that.

Never put patient information in any chat, whichever tools you use.

**Contents:** [What you need](#what-you-need) · [Claude on the web or Claude Desktop](#claude-on-the-web-or-claude-desktop) ·
[Team and Enterprise plans](#team-and-enterprise-plans) · [Claude Code](#claude-code) ·
[One-time install (for now)](#one-time-install-on-your-computer-for-now) · [IHME GBD](#ihme-gbd-connector) ·
[PubMed](#pubmed-and-biorxivmedrxiv-connectors) · [Put the rules in your Project](#put-the-rules-in-your-project) ·
[Permission prompts](#why-claude-asks-before-using-a-tool) · [Other tools](#other-optional-tools) ·
[If something goes wrong](#if-something-goes-wrong)

## What you need

- A Claude account.
- For this tool: nothing else. No account, no sign-in, no password, no download. The data is public and the tool
  only reads it.
- For the IHME GBD connector: a free IHME account, which you can create while you connect (see
  [IHME GBD](#ihme-gbd-connector)).

> [!NOTE]
> Coming: this tool's web address is not published yet. Until it is, use the
> [one-time install](#one-time-install-on-your-computer-for-now) in Claude Desktop or [Claude Code](#claude-code).

## Claude on the web or Claude Desktop

These steps are the main path once the tool's web address is published. They need no terminal and no install.

1. **Open the connector settings.** In Claude, open Settings → Connectors (on some accounts it is under
   Customize).
   You should see a list of connectors and a button to add a custom connector.
   <!-- SCREENSHOT: Claude Settings → Connectors, with "Add custom connector" visible -->
2. **Add this tool.** Click **Add custom connector**. Name it `health-data-registry`, paste the tool's web address
   (to be published here), and click **Add**.
   You should see health-data-registry in your list of connectors.
   If you see an error, check that you pasted the whole address, with nothing before or after it.
   <!-- SCREENSHOT: the Add custom connector dialog with the name and address filled in -->
3. **Start a new chat.** A connector only appears in chats started after you add it.
   <!-- SCREENSHOT: the new-chat button -->
4. **Check that it is switched on.** Click the **+** button at the bottom left of the message box, then
   **Connectors**. Make sure health-data-registry is switched on.
   <!-- SCREENSHOT: the + menu → Connectors, with health-data-registry switched on -->
5. **Run the "Start here" helper.** In the message box, open the menu of ready-made starters, choose
   health-data-registry, then **Start here: check my tools work**.
   You should see one line per tool saying whether it works, with steps for any that are missing.
   If the menu doesn't show the helpers, paste [Start here](helpers.md#start-here-check-my-tools-work) as your
   message instead.
   <!-- SCREENSHOT: the starters menu with "Start here: check my tools work" highlighted -->
6. **Ask one question,** for example: "Use health-data-registry: how many legionellosis cases were reported in
   Michigan each year since 2022?"
   You should see a table, then lines saying where the numbers came from and when.
   <!-- SCREENSHOT: a question and the sourced answer table -->

Not yet checked: whether Claude on the web and the mobile app show a connector's ready-made starters the way
Claude Desktop does. If yours doesn't, use the copy-paste versions on the [helpers page](helpers.md).

## Team and Enterprise plans

If you can't add a custom connector, your organisation's Claude owner may need to add it, or allow custom
connectors, for your team. Send them this page. Each member then switches it on in their own chats, as in steps
3 and 4 above. This tool needs no sign-in, so there is nothing else for each member to set up.

## Claude Code

Claude Code is Anthropic's coding tool. Use one of these options, not both: using both adds the tool twice.

**Option A, hosted (coming).** Once the web address is published, run:

```bash
claude mcp add --transport http health-data-registry <web address, to be published>
```

Then run `/mcp` inside Claude Code. You should see health-data-registry listed as connected.

**Option B, on your own computer (works today).** You need [uv](https://docs.astral.sh/uv/) and Git. Load the data
once (step 4 of the [one-time install](#one-time-install-on-your-computer-for-now)), then run:

```bash
claude mcp add health-data-registry --env HDR_LAKE="$HOME/health-data" -- \
  uvx --from git+https://github.com/jtouley/health-data-registry health-data-registry serve
```

Then run `/mcp`. You should see health-data-registry listed as connected. If it says failed, check that the
`health-data` folder exists and holds the data from step 4.

## One-time install on your computer (for now)

Until the web address is published, this tool needs a **one-time install** on your computer and works in the
**Claude Desktop app**. It takes about 15 minutes. You will type four commands into a **terminal**, the window where
you type commands to your computer.

Coming: once the tool is hosted, the data lives on the open health data hub and the tool reads it from there, so
this install and the "load the data" step go away.

Prefer to be guided? Paste the [Walk me through an install](helpers.md#walk-me-through-an-install) helper into
any Claude chat, with "health-data-registry" as the tool, and Claude will take you through these steps one at a
time. If any step is blocked on a work computer, ask your institution's IT team to install
[uv](https://docs.astral.sh/uv/) and Git for you.

### Before you start

- The Claude Desktop app, signed in ([claude.com/download](https://claude.com/download)).
- A Mac or a Windows computer where you are allowed to install programs.

### Steps

1. **Open a terminal.**
   On a Mac: open Finder → Applications → Utilities → Terminal.
   On Windows: open the Start menu, type "PowerShell" and open it.
   You should see a window with a blinking cursor.
   <!-- SCREENSHOT: an open Terminal window on a Mac, and PowerShell on Windows -->

2. **Install uv,** the small program that runs this tool. Paste one line and press Return:
   - Mac: `curl -LsSf https://astral.sh/uv/install.sh | sh`
   - Windows: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`

   You should see a message ending in "everything's installed!".
   Then close the terminal and open a new one, so it finds uv.
   If you see "permission denied" or "blocked by policy", ask your IT team to install uv.

3. **Check that Git is installed.** Type `git --version` and press Return.
   You should see a version number, such as "git version 2.45".
   If a Mac offers to install "command line developer tools", click Install, wait, then type it again.
   On Windows, if it says "not recognized", install Git from [git-scm.com](https://git-scm.com/download/win),
   then open a new PowerShell window.

4. **Load the data once.** This downloads the five starter datasets from CDC and WHO into a folder called
   `health-data` in your home folder. Paste one line and press Return:
   - Mac:
     `HDR_LAKE="$HOME/health-data" uvx --from git+https://github.com/jtouley/health-data-registry health-data-registry refresh --all`
   - Windows:
     `$env:HDR_LAKE="$HOME\health-data"; uvx --from git+https://github.com/jtouley/health-data-registry health-data-registry refresh --all`

   It takes about 15 minutes and needs about 1.5 GB of free disk space (most of it is the publishers' original
   files, kept so every number can be traced back). You should get your blinking cursor back with no error message.
   If you see an error naming one dataset, the publisher's site may be down; try again later. The other
   datasets still work.

5. **Note two paths.** Type the line for your computer and press Return:
   - Mac: `which uvx; echo $HOME/health-data`
   - Windows: `where.exe uvx; echo $HOME\health-data`

   You should see two lines, for example `/Users/yourname/.local/bin/uvx` and `/Users/yourname/health-data`.
   Keep this window open; you need both lines in the next step.

6. **Add the tool to Claude Desktop.** In Claude Desktop, open Settings → Developer → Edit Config. A file called
   `claude_desktop_config.json` opens in a text editor. Replace its contents with the text below, putting your
   two lines from step 5 where it says so. If the file already lists other tools, ask Claude to help you merge
   the two, using the [Something's wrong](helpers.md#somethings-wrong) helper.

   ```json
   {
     "mcpServers": {
       "health-data-registry": {
         "command": "FIRST LINE FROM STEP 5",
         "args": ["--from", "git+https://github.com/jtouley/health-data-registry",
                  "health-data-registry", "serve"],
         "env": { "HDR_LAKE": "SECOND LINE FROM STEP 5" }
       }
     }
   }
   ```

   On Windows, write each `\` in the paths as `\\`. Save the file.
   <!-- SCREENSHOT: Claude Desktop, Settings → Developer, with the Edit Config button -->

7. **Quit Claude Desktop fully and open it again.** On a Mac, use Claude → Quit Claude (closing the window isn't
   enough). On Windows, right-click the Claude icon by the clock and choose Quit.
   You should see health-data-registry under Settings → Developer, marked as running.
   If it says "failed", check the two paths in step 6 for typos, then quit and reopen again.
   <!-- SCREENSHOT: Settings → Developer showing health-data-registry running -->

8. **Start a new chat and test it.** A tool only appears in chats started after you add it. Open the menu of
   ready-made starters in the message box, choose health-data-registry, then **Start here: check my tools work**.
   You should see one line per tool saying whether it works.
   If the starters don't show in the menu, paste [Start here](helpers.md#start-here-check-my-tools-work) as your
   message instead.
   <!-- SCREENSHOT: the starters menu with "Start here: check my tools work" -->

**To get newer data later,** repeat step 4. Each dataset shows the date it was fetched with every answer.

## IHME GBD connector

GBD gives modelled burden of disease, 1990–2023, with uncertainty ranges. These steps follow IHME's own page,
[Explore GBD 2023 study estimates with Claude](https://www.healthdata.org/explore-gbd-2023-study-estimates-claude),
and the [fellows' guide](https://claude.ai/artifact/S8n9EHbM7NQ5HmC6UNmxG6). It works in Claude on the web, Claude
Desktop and the mobile app. You need a free IHME account; allow about 5 minutes and have your email inbox open.

1. **Find it.** In Claude, open Settings → Connectors (or Customize), click the **Discover** tab, type IHME GBD and
   press Enter. You can also go straight to
   [claude.com/connectors/ihme-gbd](https://claude.com/connectors/ihme-gbd).
   <!-- SCREENSHOT: the IHME GBD connector page with the Connect button -->
2. **Connect.** Click IHME GBD, then **Connect to Claude**. An IHME sign-in page opens.
3. **Sign in or register.** Sign in with your IHME account, or click **Register** and follow the form. You won't
   be connected until you have signed in.
4. **Return to Claude.** In Claude Desktop you may see "Finish connecting a connector?". Click **Continue
   connecting**.
5. **Start a new chat** and check the connector is switched on under **+** → **Connectors**.
6. **Test it:** type "use the GBD tool. top causes of death in Michigan".
   You should see a table. If you don't, run the [Something's wrong](helpers.md#somethings-wrong) helper.

If IHME GBD later shows "Sign-in expired" or a warning icon, open its connector page again and click
**Reconnect**. In Claude Code, IHME's page gives
`claude mcp add --transport http ihme-gbd https://gbdmcp.healthdata.org/mcp`, then `/mcp` and **Authenticate**.

## PubMed and bioRxiv/medRxiv connectors

PubMed finds published papers; bioRxiv and medRxiv find preprints. These steps come from Anthropic's
[PubMed connector tutorial](https://claude.com/resources/tutorials/using-the-pubmed-connector-in-claude)
(**not tested here**).

1. In Claude, open Settings → Connectors, find **PubMed**, and click Connect. Do the same for bioRxiv and medRxiv
   if you want preprints.
   <!-- SCREENSHOT: Settings → Connectors with PubMed and its Connect button -->
2. Start a new chat.
3. Test it: type "use PubMed: recent studies on community-onset C. difficile in older adults".
   You should see a list of papers with links.

## Put the rules in your Project

Once your tools work, run the **Set up my Claude Project** helper (`set_up_my_project`, or copy it from the
[helpers page](helpers.md#set-up-my-claude-project)). It writes the Project instructions that keep each tool in its
lane and tells you where to paste them.

## Why Claude asks before using a tool

By default, Claude asks for your approval before a connector runs. This is a safety feature, not a fault. In the
connector's settings in Claude you can choose to be asked every time, or to allow it for the current chat or
always. Every tool here only reads public data, so allowing it can't change anything.

## Other optional tools

Data Commons (harmonised public statistics) and an unofficial OpenEvidence add-on both need an install in Claude
Desktop. They are optional and not part of this project. The
[Walk me through an install](helpers.md#walk-me-through-an-install) helper guides you step by step. Read
OpenEvidence's terms before using the unofficial add-on: whether they allow automated use is **not confirmed**.

## If something goes wrong

Run the [Something's wrong](helpers.md#somethings-wrong) helper and describe what you see. The most common fixes:

- **The tool isn't in my chat:** start a new chat. Tools added during a chat don't appear in it. Then check it is
  switched on under **+** → **Connectors**.
- **Claude Desktop says the tool failed (one-time install):** check the two paths in step 6, then quit Claude
  Desktop fully and open it again. Claude Desktop's own logs are in `~/Library/Logs/Claude` (Mac) or
  `%APPDATA%\Claude\logs` (Windows); you can paste the last lines into a chat and ask Claude what they mean.
- **An answer says the data "hasn't been downloaded on this computer yet":** repeat step 4 of the one-time
  install.
- **A number looks wrong:** ask Claude to show the provenance (`get_provenance`) and check it against the
  publisher's page it links to. If the publisher's page disagrees, trust the publisher and
  [open an issue](https://github.com/jtouley/health-data-registry/issues).
- **Anything asks for a password or your login for this tool:** stop. This tool never needs one. (The IHME GBD
  connector does ask you to sign in to IHME; that is expected.)
