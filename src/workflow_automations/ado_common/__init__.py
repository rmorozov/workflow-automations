"""Helpers shared by the Azure DevOps tools (ado-graph and ado-triage).

Modules here carry no tool's policy for missing or failed work items. None makes
requests except `session`, which builds the Kerberos connection and run-scoped CA
bundle both tools use. `options` gives both command lines options files
(ConfigArgParse); the others are pure.
"""
