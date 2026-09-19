Transcript (turn_index: speaker: text):

{transcript_turns}

Perform thought construction over every turn above, following the rules in
your system instructions. Remember: `text_span` must be a verbatim substring
of the turn it cites, a turn may yield several thoughts or none, and a
statement later contradicted still produces its own thought with its own
polarity.

Output only the JSON object with the single top-level key "thoughts".
