"""Skills Hub — Browse and install skills from Hermes/ClawHub/OpenClaw.

Reads skills from:
1. Local Hermes repo (/root/openclaw/hermes/skills/) — 95+ skills
2. ClawHub API (https://clawhub.ai/api/v1) — community skills
3. Hermes optional-skills (/root/openclaw/hermes/optional-skills/)

Installed skills land in skills/hub/ as SKILL.md files that agents can
reference as knowledge/procedures.
"""

import json
import re
import shutil
from pathlib import Path
from typing import Optional

import httpx
import yaml

from core.logger import log

# Paths
HERMES_SKILLS = Path("/root/openclaw/hermes/skills")
HERMES_OPTIONAL = Path("/root/openclaw/hermes/optional-skills")
PAWANG_HUB_DIR = Path(__file__).parent / "hub"
PAWANG_HUB_DIR.mkdir(exist_ok=True)

CLAWHUB_API = "https://clawhub.ai/api/v1"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class SkillInfo:
    """Lightweight skill metadata."""
    __slots__ = ("name", "description", "category", "source", "path", "identifier", "tags")

    def __init__(self, name: str, description: str = "", category: str = "",
                 source: str = "", path: str = "", identifier: str = "",
                 tags: list = None):
        self.name = name
        self.description = description
        self.category = category
        self.source = source
        self.path = path
        self.identifier = identifier or f"{source}/{category}/{name}"
        self.tags = tags or []

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description[:120],
            "category": self.category,
            "source": self.source,
            "identifier": self.identifier,
        }


# ---------------------------------------------------------------------------
# Local Hermes scanner
# ---------------------------------------------------------------------------

def _parse_skill_md(path: Path) -> Optional[SkillInfo]:
    """Parse a SKILL.md file and return SkillInfo."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        if not content.startswith("---"):
            return None
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None
        meta = yaml.safe_load(parts[1]) or {}
        if not isinstance(meta, dict):
            return None

        name = meta.get("name", path.parent.name)
        desc = meta.get("description", "")
        tags_raw = meta.get("metadata", {})
        if isinstance(tags_raw, dict):
            hermes_meta = tags_raw.get("hermes", {})
            tags = hermes_meta.get("tags", []) if isinstance(hermes_meta, dict) else []
        else:
            tags = []

        # Derive category from path: skills/<category>/<skill>/SKILL.md
        rel = path.relative_to(path.parents[2]) if len(path.parts) > 3 else path
        category = rel.parts[0] if len(rel.parts) > 2 else ""

        return SkillInfo(
            name=name, description=desc, category=category,
            source="hermes", path=str(path), tags=tags,
        )
    except Exception as e:
        log.warning(f"Failed to parse {path}: {e}")
        return None


def scan_hermes_skills() -> list[SkillInfo]:
    """Scan local Hermes skills directory."""
    skills = []
    for skill_dir in [HERMES_SKILLS, HERMES_OPTIONAL]:
        if not skill_dir.exists():
            continue
        for md in skill_dir.rglob("SKILL.md"):
            info = _parse_skill_md(md)
            if info:
                skills.append(info)
    return skills


# ---------------------------------------------------------------------------
# ClawHub API
# ---------------------------------------------------------------------------

async def search_clawhub(query: str, limit: int = 10) -> list[SkillInfo]:
    """Search ClawHub API for skills."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{CLAWHUB_API}/skills",
                params={"q": query, "limit": limit},
            )
            if resp.status_code != 200:
                log.warning(f"ClawHub search failed: {resp.status_code}")
                return []

            data = resp.json()
            items = data if isinstance(data, list) else data.get("skills", data.get("results", []))

            skills = []
            for item in items[:limit]:
                if not isinstance(item, dict):
                    continue
                # Handle nested {skill: {...}} format
                if "skill" in item and isinstance(item["skill"], dict):
                    item = item["skill"]
                skills.append(SkillInfo(
                    name=item.get("name", "unknown"),
                    description=item.get("description", ""),
                    category=item.get("category", ""),
                    source="clawhub",
                    identifier=f"clawhub/{item.get('name', '')}",
                    tags=item.get("tags", []) if isinstance(item.get("tags"), list) else [],
                ))
            return skills
    except Exception as e:
        log.warning(f"ClawHub search error: {e}")
        return []


# ---------------------------------------------------------------------------
# Search (unified)
# ---------------------------------------------------------------------------

def search_local(query: str, limit: int = 20) -> list[SkillInfo]:
    """Search local Hermes skills by name/description/tags."""
    query_lower = query.lower().strip()
    all_skills = scan_hermes_skills()

    if not query_lower:
        return all_skills[:limit]

    # Score each skill
    scored = []
    terms = query_lower.split()
    for s in all_skills:
        score = 0
        name_lower = s.name.lower()
        desc_lower = s.description.lower()
        cat_lower = s.category.lower()
        tags_lower = " ".join(t.lower() for t in s.tags)

        # Exact name match
        if query_lower == name_lower:
            score += 100
        # Name contains query
        elif query_lower in name_lower:
            score += 50
        # Check individual terms
        for term in terms:
            if term in name_lower:
                score += 30
            if term in desc_lower:
                score += 10
            if term in cat_lower:
                score += 15
            if term in tags_lower:
                score += 20

        if score > 0:
            scored.append((score, s))

    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:limit]]


async def search_all(query: str, limit: int = 15) -> list[SkillInfo]:
    """Search both local Hermes and ClawHub."""
    local = search_local(query, limit=limit)

    # Only hit ClawHub if query is specific enough
    clawhub = []
    if query and len(query) >= 3:
        clawhub = await search_clawhub(query, limit=limit)

    # Merge: local first, then clawhub (deduplicated)
    seen = {s.name for s in local}
    merged = list(local)
    for s in clawhub:
        if s.name not in seen:
            merged.append(s)
            seen.add(s.name)

    return merged[:limit]


# ---------------------------------------------------------------------------
# Browse by category
# ---------------------------------------------------------------------------

def browse_categories() -> dict[str, int]:
    """List available categories and skill counts."""
    skills = scan_hermes_skills()
    cats: dict[str, int] = {}
    for s in skills:
        cat = s.category or "uncategorized"
        cats[cat] = cats.get(cat, 0) + 1
    return dict(sorted(cats.items()))


def browse_category(category: str) -> list[SkillInfo]:
    """List all skills in a category."""
    skills = scan_hermes_skills()
    return [s for s in skills if s.category.lower() == category.lower()]


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

def get_installed() -> list[dict]:
    """List installed hub skills."""
    installed = []
    if not PAWANG_HUB_DIR.exists():
        return installed
    for md in PAWANG_HUB_DIR.rglob("SKILL.md"):
        info = _parse_skill_md(md)
        if info:
            installed.append(info.to_dict())
    return installed


def install_skill(identifier_or_name: str) -> dict:
    """Install a skill from local Hermes repo.

    Copies the skill directory to skills/hub/<name>/.
    Returns {success, name, message}.
    """
    # Find skill by name or identifier
    all_skills = scan_hermes_skills()

    target = None
    name_lower = identifier_or_name.lower().strip()
    for s in all_skills:
        if s.name.lower() == name_lower:
            target = s
            break
        if name_lower in s.identifier.lower():
            target = s
            break

    if not target:
        # Try partial match
        for s in all_skills:
            if name_lower in s.name.lower():
                target = s
                break

    if not target:
        return {
            "success": False,
            "name": identifier_or_name,
            "message": f"Skill '{identifier_or_name}' tidak ditemukan. Coba 'skill_hub search <keyword>'.",
        }

    # Source directory (parent of SKILL.md)
    src_dir = Path(target.path).parent
    if not src_dir.exists():
        return {
            "success": False,
            "name": target.name,
            "message": f"Source directory not found: {src_dir}",
        }

    # Destination
    dest_dir = PAWANG_HUB_DIR / target.name
    if dest_dir.exists():
        return {
            "success": False,
            "name": target.name,
            "message": f"Skill '{target.name}' sudah terinstall di {dest_dir}. Hapus dulu jika ingin reinstall.",
        }

    # Copy skill directory
    try:
        shutil.copytree(src_dir, dest_dir)
        log.info(f"Installed skill: {target.name} from {src_dir} → {dest_dir}")

        # Count files
        files = list(dest_dir.rglob("*"))
        file_count = sum(1 for f in files if f.is_file())

        return {
            "success": True,
            "name": target.name,
            "message": (
                f"✅ Skill '{target.name}' berhasil diinstall.\n"
                f"Source: {target.source}/{target.category}\n"
                f"Path: {dest_dir}\n"
                f"Files: {file_count}\n"
                f"Description: {target.description[:200]}"
            ),
        }
    except Exception as e:
        return {
            "success": False,
            "name": target.name,
            "message": f"Install error: {e}",
        }


def uninstall_skill(name: str) -> dict:
    """Remove an installed hub skill."""
    dest_dir = PAWANG_HUB_DIR / name
    if not dest_dir.exists():
        return {"success": False, "message": f"Skill '{name}' tidak terinstall."}

    try:
        shutil.rmtree(dest_dir)
        log.info(f"Uninstalled skill: {name}")
        return {"success": True, "message": f"✅ Skill '{name}' berhasil dihapus."}
    except Exception as e:
        return {"success": False, "message": f"Uninstall error: {e}"}


def read_skill(name: str) -> str:
    """Read the full SKILL.md content of an installed skill."""
    # Check installed first
    dest_dir = PAWANG_HUB_DIR / name
    md_path = dest_dir / "SKILL.md"
    if md_path.exists():
        return md_path.read_text(encoding="utf-8", errors="replace")

    # Check local Hermes
    for s in scan_hermes_skills():
        if s.name.lower() == name.lower():
            path = Path(s.path)
            if path.exists():
                return path.read_text(encoding="utf-8", errors="replace")

    return f"Skill '{name}' tidak ditemukan."


# ---------------------------------------------------------------------------
# Tool entry point (called from core/tools.py)
# ---------------------------------------------------------------------------

async def execute_skill_hub(action: str, query: str = "", name: str = "") -> str:
    """Main entry point for the skill_hub tool.

    Actions:
        search <query>  — Search skills across Hermes + ClawHub
        browse          — Browse categories
        browse <cat>    — List skills in a category
        list            — List installed skills
        install <name>  — Install a skill
        uninstall <name> — Remove a skill
        read <name>     — Read skill content
        info <name>     — Show skill metadata
    """
    action = action.lower().strip()

    if action == "search":
        results = await search_all(query or "")
        if not results:
            return "Tidak ditemukan skill yang cocok."
        lines = [f"🔍 Ditemukan {len(results)} skill:\n"]
        for i, s in enumerate(results, 1):
            lines.append(f"{i}. **{s.name}** ({s.source}/{s.category})")
            if s.description:
                lines.append(f"   {s.description[:100]}")
        lines.append(f"\nGunakan `skill_hub install <nama>` untuk install.")
        return "\n".join(lines)

    elif action == "browse":
        if query:
            # Browse specific category
            skills = browse_category(query)
            if not skills:
                return f"Kategori '{query}' tidak ditemukan atau kosong."
            lines = [f"📂 Kategori: {query} ({len(skills)} skills)\n"]
            for s in skills:
                lines.append(f"• **{s.name}** — {s.description[:80]}")
            return "\n".join(lines)
        else:
            # Browse all categories
            cats = browse_categories()
            lines = ["📂 Kategori skill yang tersedia:\n"]
            for cat, count in cats.items():
                lines.append(f"• **{cat}** — {count} skill(s)")
            lines.append(f"\nTotal: {sum(cats.values())} skills")
            lines.append("Gunakan `skill_hub browse <kategori>` untuk lihat isi.")
            return "\n".join(lines)

    elif action == "list":
        installed = get_installed()
        if not installed:
            return "Belum ada skill yang terinstall dari Hub."
        lines = [f"📦 {len(installed)} skill terinstall:\n"]
        for s in installed:
            lines.append(f"• **{s['name']}** ({s['source']}/{s['category']})")
            if s.get("description"):
                lines.append(f"  {s['description'][:80]}")
        return "\n".join(lines)

    elif action == "install":
        target = name or query
        if not target:
            return "Error: nama skill diperlukan. Contoh: skill_hub install notion"
        result = install_skill(target)
        return result["message"]

    elif action == "uninstall":
        target = name or query
        if not target:
            return "Error: nama skill diperlukan."
        result = uninstall_skill(target)
        return result["message"]

    elif action == "read":
        target = name or query
        if not target:
            return "Error: nama skill diperlukan."
        content = read_skill(target)
        # Truncate if too long
        if len(content) > 3000:
            content = content[:3000] + "\n\n... (truncated, full content di file SKILL.md)"
        return content

    elif action == "info":
        target = name or query
        if not target:
            return "Error: nama skill diperlukan."
        all_skills = scan_hermes_skills()
        for s in all_skills:
            if s.name.lower() == target.lower():
                return (
                    f"📋 **{s.name}**\n"
                    f"Description: {s.description}\n"
                    f"Category: {s.category}\n"
                    f"Source: {s.source}\n"
                    f"Tags: {', '.join(s.tags)}\n"
                    f"Path: {s.path}\n"
                    f"Identifier: {s.identifier}"
                )
        return f"Skill '{target}' tidak ditemukan."

    else:
        return (
            "Aksi tidak dikenal. Gunakan salah satu:\n"
            "• skill_hub search <keyword>\n"
            "• skill_hub browse [kategori]\n"
            "• skill_hub list\n"
            "• skill_hub install <nama>\n"
            "• skill_hub uninstall <nama>\n"
            "• skill_hub read <nama>\n"
            "• skill_hub info <nama>"
        )
