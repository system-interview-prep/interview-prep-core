"""Candidate memory: LangMem retrieval and extraction (V2 spec §9, §10).

Retrieval runs on the hot path (recent turns, skill retrieval, previous gaps);
extraction runs in the background after the report is stored. Memory never
drives the agent, and namespaces are per user/session.

``candidate_memory.py`` holds the store, the seed, the search tool, the prompt
block, the guard and the extractor together: §10 lists four files for this
package, but the module is one cohesive ~490-line unit and the spec does not
ask for empty files.
"""
