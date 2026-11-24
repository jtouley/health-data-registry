# Setting up your research tools

This page gets you from nothing to a working chat. It covers this tool (health-data-registry) and the two
connectors it works best with: IHME GBD and PubMed.

A **connector** is an add-on that lets Claude use an outside tool. You add each one once.

Never put patient information in any chat, whichever tools you use.

**Contents:** [This tool](#this-tool-health-data-registry) · [IHME GBD](#ihme-gbd-connector) ·
[PubMed](#pubmed-and-biorxivmedrxiv-connectors) · [Other tools](#other-optional-tools) ·
[If something goes wrong](#if-something-goes-wrong)

## This tool (health-data-registry)

There is no web address to paste yet, so for now this tool needs a **one-time install** on your computer and works
in the **Claude Desktop app** only. It takes about 15 minutes. You will type four commands into a **terminal**, the
window where you type commands to your computer.

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

### Once the tool is hosted

These steps will replace the install. Nothing to do yet.

1. In Claude, open Settings → Connectors → Add custom connector.
2. Paste the tool's web address (to be published here) and click Add.
3. Start a new chat and run **Start here**.

## IHME GBD connector

GBD gives modelled burden of disease, 1990–2023, with uncertainty ranges. These are the steps from the
[fellows' guide](https://claude.ai/artifact/S8n9EHbM7NQ5HmC6UNmxG6). It works in Claude on the web, Claude Desktop
and the mobile app.

1. Open [claude.com/connectors/ihme-gbd](https://claude.com/connectors/ihme-gbd), click **Add to Claude**, and sign
   in.
   <!-- SCREENSHOT: the IHME GBD connector page with the Add to Claude button -->
2. Start a new chat. The tool only appears in chats started after you add it.
3. Test it: type "use the GBD tool. top causes of death in Michigan".
   You should see a table. If you don't, run the [Something's wrong](helpers.md#somethings-wrong) helper.

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

## Other optional tools

Data Commons (harmonised public statistics) and an unofficial OpenEvidence add-on both need an install in Claude
Desktop. They are optional and not part of this project. The
[Walk me through an install](helpers.md#walk-me-through-an-install) helper guides you step by step. Read
OpenEvidence's terms before using the unofficial add-on: whether they allow automated use is **not confirmed**.

## If something goes wrong

Run the [Something's wrong](helpers.md#somethings-wrong) helper and describe what you see. The most common fixes:

- **The tool isn't in my chat:** start a new chat. Tools added during a chat don't appear in it.
- **Claude Desktop says the tool failed:** check the two paths in step 6, then quit Claude Desktop fully and open
  it again. Claude Desktop's own logs are in `~/Library/Logs/Claude` (Mac) or `%APPDATA%\Claude\logs` (Windows);
  you can paste the last lines into a chat and ask Claude what they mean.
- **An answer says a dataset "has no local snapshot yet":** repeat step 4.
- **Anything asks for a password or your login:** stop. This tool never needs one.

Not yet checked: whether Claude on the web and the mobile app show a connector's ready-made starters the way
Claude Desktop does. If yours doesn't, use the copy-paste versions on the [helpers page](helpers.md).
