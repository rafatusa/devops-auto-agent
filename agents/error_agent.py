"""
Error Agent — pure Python, no AI
Reads pipeline logs and extracts error context.
Passes raw error to code_agent for AI analysis and fix.
"""
import re
import logging

logger = logging.getLogger(__name__)


class ErrorAgent:

    def analyze(self, failed_jobs: list) -> dict:
        """
        Collect all failed job logs and return combined context.
        No pattern matching — just extract the most relevant log sections.
        """
        if not failed_jobs:
            return {"error": "No failed jobs", "log_context": "", "file": None}

        all_logs = []
        job_name = ""

        for job in failed_jobs:
            log  = job.get("log", "")
            name = job.get("name", "")
            if log:
                all_logs.append(f"=== Job: {name} ===\n{log}")
                job_name = name

        combined_log = "\n\n".join(all_logs)

        # Extract the most relevant error section (last 3000 chars around "Error:")
        error_context = self._extract_error_section(combined_log)

        return {
            "job_name":    job_name,
            "log_context": error_context,
            "full_log":    combined_log[-5000:],
            "file":        None,   # code_agent will determine this
            "error":       None,   # code_agent will determine this
        }

    def _extract_error_section(self, log: str) -> str:
        """Extract lines around error keywords."""
        lines = log.splitlines()
        error_indices = []

        for i, line in enumerate(lines):
            l = line.lower()
            if any(kw in l for kw in ["error:", "failed:", "fatal:", "exception", "unsupported:", "invalid"]):
                error_indices.append(i)

        if not error_indices:
            return log[-3000:]

        # Get context around first and last error
        start = max(0, error_indices[0] - 5)
        end   = min(len(lines), error_indices[-1] + 15)
        return "\n".join(lines[start:end])

    def format_for_user(self, analysis: dict) -> str:
        return (
            f"Pipeline failed at: {analysis.get('job_name', 'unknown')}\n"
            f"Sending to code agent for analysis..."
        )


error_agent = ErrorAgent()