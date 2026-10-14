"""Helpers shared by the Azure DevOps tools (ado-graph and ado-triage).

Modules here carry no tool's policy for missing or failed work items. All but
`session` are pure and make no requests; `session` builds the Kerberos connection
and run-scoped CA bundle both tools use.
"""
