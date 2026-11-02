"""Stealth task crawler simulating realistic human navigation."""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any

from ..client import ManageBacClient
from ..filters import is_submitted_badge
from .events import StealthConfig

log = logging.getLogger(__name__)


class StealthTaskCrawler:
    """Enriches tasks with detailed metadata while adhering to human-like browsing hierarchy."""

    def __init__(self, client: ManageBacClient, config: StealthConfig | None = None):
        self.client = client
        self.config = config or StealthConfig()

    def _jitter(self) -> None:
        if self.config.enabled:
            delay = random.uniform(
                self.config.min_jitter_seconds, self.config.max_jitter_seconds
            )
            time.sleep(delay)

    def fetch_task_details(
        self, class_id: int | str, task_id: int | str
    ) -> dict[str, Any] | None:
        """Fetch full task details by navigating class workspace -> task page."""
        class_id_str = str(class_id)
        task_id_str = str(task_id)

        # 1. Stealthily visit class parent context first if configured
        if self.config.enabled and self.config.fetch_parent_context:
            try:
                cal_path = f"/student/classes/{class_id_str}/calendar"
                log.debug("Stealth visiting parent class calendar: %s", cal_path)
                self.client._get(cal_path, bypass_cache=True)
                self._jitter()
            except Exception as exc:
                log.debug("Parent calendar fetch skipped: %s", exc)

        # 2. Fetch the target task page
        task_path = f"/student/classes/{class_id_str}/core_tasks/{task_id_str}"
        try:
            log.debug("Fetching task details from %s", task_path)
            soup = self.client._get(task_path, bypass_cache=True)
            self._jitter()
        except Exception as exc:
            log.warning("Failed to fetch task %s: %s", task_path, exc)
            return None

        # 3. Parse class name first so title extraction can guard against matching it
        class_name = ""
        class_el = soup.find("a", href=lambda h: h and f"/student/classes/{class_id_str}" in h)
        if class_el:
            class_name = class_el.get_text(strip=True)
        if not class_name:
            hero_el = soup.find(class_=re.compile(r"f-hero__title"))
            if hero_el:
                class_name = hero_el.get_text(strip=True)

        # Parse task title
        title = ""
        # 3a. Primary: .fusion-card-item .title (e.g. <div class="h4 title">)
        card = soup.find(class_=("short-assignment", "fusion-card-item"))
        if card:
            title_node = card.find(class_=re.compile(r"\btitle\b"))
            if title_node:
                # Remove popovers, icons, and badges before extracting text
                for noise in title_node.find_all(class_=re.compile(r"popover|icon|badge|indicator", re.I)):
                    noise.decompose()
                candidate = title_node.get_text(strip=True)
                if candidate and candidate.lower() != class_name.lower():
                    title = candidate

        # 3b. Fallback: Breadcrumb navigation (last item)
        if not title:
            breadcrumb = soup.find(id="breadcrumb") or soup.find(class_=re.compile(r"breadcrumb", re.I))
            if breadcrumb:
                items = [li.get_text(strip=True) for li in breadcrumb.find_all("li") if li.get_text(strip=True)]
                if items:
                    last = items[-1]
                    if last.lower() not in ("tasks", "task details", "calendar", class_name.lower()):
                        title = last

        # 3c. Fallback: Headings with class="title", explicitly excluding hero title and modal titles
        if not title:
            for el in soup.find_all(["h1", "h2", "h3", "h4"], class_=re.compile(r"\btitle\b")):
                if any(c in el.get("class", []) for c in ["f-hero__title", "modal-title", "offcanvas-title"]):
                    continue
                candidate = el.get_text(strip=True)
                if candidate and candidate.lower() != class_name.lower():
                    title = candidate
                    break

        if not title:
            title = f"Task {task_id_str}"

        # Due date
        due_date_str = ""
        due_match = re.search(r"Due:\s*([^\n\r<]+)", soup.get_text())
        if due_match:
            due_date_str = due_match.group(1).strip()
        else:
            due_el = soup.find(lambda el: el.name in ("p", "div", "span", "time") and "Due" in el.get_text())
            if due_el:
                due_date_str = re.sub(r"^Due:\s*", "", due_el.get_text(strip=True), flags=re.IGNORECASE)

        # Check submission button & status
        has_submit_btn = bool(
            soup.find("a", href=lambda h: h and "dropbox" in h)
            or soup.find("input", {"type": "submit"})
        )

        status = "not-submitted"

        # Check for submitted indicators
        badges = [
            el.get_text(strip=True).lower()
            for el in soup.find_all(
                class_=lambda c: c and any(k in c for k in ["badge", "label", "status"])
            )
        ]
        if any(is_submitted_badge(b) for b in badges):
            status = "submitted"

        # Check if dropbox shows a file uploaded
        if has_submit_btn and status != "submitted":
            try:
                submissions = self.client.get_submissions(class_id_str, task_id_str)
                self._jitter()
                if submissions and not submissions[0].get("error"):
                    status = "submitted"
            except Exception:
                pass

        # Grade — mirrors get_teacher_feedback() selectors on the same page
        grade_letter = None
        grade_score = None
        _noise = ("submitted", "pending", "task", "due", "not")
        card = soup.find(class_=("short-assignment", "fusion-card-item"))
        if card:
            grade_el = card.find(class_=re.compile(r"\bgrade\b"))
            if grade_el:
                grade_letter = grade_el.get_text(strip=True) or None
            points_el = card.find("div", class_="points")
            if points_el:
                raw_pt = points_el.get_text(strip=True)
                if raw_pt and not any(k in raw_pt.lower() for k in _noise):
                    grade_score = raw_pt

        return {
            "id": task_id_str,
            "task_id": task_id_str,
            "title": title,
            "class_id": class_id_str,
            "class_name": class_name,
            "due_date": due_date_str,
            "status": status,
            "has_submit_button": has_submit_btn,
            "grade_letter": grade_letter,
            "grade_score": grade_score,
            "url": f"{self.client.base}{task_path}",
        }
