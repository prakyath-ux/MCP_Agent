Summary of changes from v1:

Change	Lever	What	Token Impact
Concise reasoning format	2	Short THOUGHT for simple actions, verbose only on failures	Saves ~$0.03-0.05/page in output
Reduced examples from 5→3	2	Cut API dropdown + page navigation examples	Saves ~400 tok/turn in system prompt
Upload: check hidden input first	1	Example shows DOM check before clicking buttons	Saves ~5 turns
Skip correct defaults	1	Don't re-test country code if already correct	Saves ~6 turns
Fast-path retries	1	Jump to JS fallback for masked inputs immediately	Saves ~5 turns
Close dropdowns before snapshot	3	Prevents 19K token bloat from country lists	Saves ~19K tokens in context
Shorter ambiguity section	2	Only use INTERPRETATIONS for genuinely unclear elements	Saves output tokens


# snapshot_filter.py
What snapshot_filter.py does
When the agent calls take_snapshot, Chrome DevTools MCP returns the full accessibility tree — every element on the page. Here's what a raw snapshot looks like (simplified):


Raw snapshot from MCP (~2,000-20,000 tokens):

  heading "TECU Credit Union - New Member Application"
  image "logo.png"                              <-- decorative, useless
  generic "wrapper"                             <-- structural div, useless
    generic "form-container"                    <-- structural div, useless
      generic "upload-section"
        button "Add profile picture" uid=1_79
        image "avatar-placeholder.svg"          <-- decorative
      heading "Personal Information"
      generic "field-row"                       <-- structural wrapper
        text "First Name *"
        textbox "First Name" uid=1_93 required
      generic "field-row"                       <-- structural wrapper
        text "Last Name *"
        textbox "Last Name" uid=1_102 required
      generic "country-dropdown" uid=1_121
        option "Afghanistan (+93)"              \
        option "Albania (+355)"                  |
        option "Algeria (+213)"                  |
        ... (187 more countries)                 |-- 190 options = ~19,000 tokens!
        option "Zimbabwe (+263)"                /
      button "Save & Continue" uid=1_30
  generic "footer"                              <-- decorative
    link "Privacy Policy"                       <-- not testing this
    link "Terms of Service"                     <-- not testing this
    text "2026 TECU Credit Union"               <-- decorative
Our filter trims this before the agent sees it. The agent gets a clean, small version:


Filtered snapshot (~200-500 tokens):

  heading "TECU Credit Union - New Member Application"
  button "Add profile picture" uid=1_79
  heading "Personal Information"
  textbox "First Name" uid=1_93 [empty, required]
  textbox "Last Name" uid=1_102 [empty, required]
  dropdown "Country Code" uid=1_121 [190 options, showing "+1868 Trinidad"]
  button "Save & Continue" uid=1_30
  progress "Step 1 of 6"
19,000 tokens → 200 tokens. Same information the agent needs. No junk.

Where it runs in the flow

MCP returns snapshot
        |
        v
  snapshot_filter.py    <-- trims it here
        |
        v
  Agent sees clean version


What it does, line by line:
Rule	What gets removed	Tokens saved
DECORATIVE_ROLES	Images, separators, presentation elements	~50-200/snapshot
STRUCTURAL_ROLES	Empty divs/spans (wrappers with no text)	~100-300/snapshot
Option collapsing	190 country options → "5 shown + 185 more"	~18,000 for country dropdown
Empty lines	Blank lines between elements	~20-50/snapshot


The big win
On the v1 run, turn 26 had 19,761 new tokens because the country dropdown opened and the snapshot included all 190 countries. With this filter, that same snapshot would add ~200 tokens instead. That's a 99% reduction on the single most expensive moment in the entire run.



# compactor.py
What compactor.py does
This is the core of Lever 4. It sits between turns and does two things:

Removes old turns — keeps only the last N turns (agent needs recent context for its current action)
Builds a rolling summary — replaces all removed turns with one short message: "Here's what you've done so far"
Filters snapshots — runs snapshot_filter on any tool results that contain accessibility trees

BEFORE compaction (what SDK would send):

  [user: task]
  [assistant: thought + tool_call(navigate)]
  [tool: navigation result]                     <-- old, remove
  [assistant: thought + tool_call(snapshot)]
  [tool: full page snapshot 2000 tokens]        <-- old, remove
  [assistant: thought + tool_call(fill)]
  [tool: fill result]                           <-- old, remove
  ... (40 more turns) ...
  [assistant: thought + tool_call(snapshot)]     <-- KEEP (recent)
  [tool: latest snapshot]                        <-- KEEP (recent)

AFTER compaction:

  [user: task]
  [user: "CONTEXT: Photo uploaded. FirstName=ROMAN. 5/7 fields done. 1 bug found."]
  [assistant: thought + tool_call(snapshot)]     <-- kept
  [tool: latest snapshot (filtered)]             <-- kept + trimmed


# orchestrator.py
How it differs from v1 openai_agent.py:
Aspect	v1	v2 orchestrator
SDK call	Runner.run(max_turns=120) one shot	Runner.run(max_turns=1) per turn in a loop
Between turns	No control	Compact history + update summary + budget check
StateTracker	Built, never used	Wired in — start_run(), end_run()
Budget	No enforcement	Real-time cost check, hard stop at $5
Context growth	6K → 53K (staircase)	~9-12K flat (compacted)
Output files	output_poc_*.txt	output_v2_poc_*.txt (dashboard compatible)
Rolling summary	None	Printed in terminal + saved in output