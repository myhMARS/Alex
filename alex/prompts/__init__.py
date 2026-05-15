"""Prompt template management using Jinja2."""

from pathlib import Path

from jinja2 import ChoiceLoader, Environment, FileSystemLoader

PROMPT_DIR = Path(__file__).parent
SKILLS_DIR = Path.home() / ".alex" / "skills"
SKILLS_PROMPT_DIR = SKILLS_DIR / "prompts"

SKILLS_PROMPT_DIR.mkdir(parents=True, exist_ok=True)

_env = Environment(
    loader=ChoiceLoader([
        FileSystemLoader(str(SKILLS_PROMPT_DIR)),
        FileSystemLoader(str(PROMPT_DIR)),
    ]),
    autoescape=False,
    keep_trailing_newline=True,
)


def render(template_name: str, **kwargs) -> str:
    """Render a prompt template with the given variables."""
    template = _env.get_template(template_name)
    return template.render(**kwargs)


def get_system_prompt(**kwargs) -> str:
    """Render the system prompt template."""
    return render("system_prompt.j2", **kwargs)


def get_reflection_prompt(**kwargs) -> str:
    """Render the reflection prompt template."""
    return render("reflection_prompt.j2", **kwargs)


def get_skills_section(**kwargs) -> str:
    """Render the skills section template."""
    return render("skills_section.j2", **kwargs)


def get_skill_card(**kwargs) -> str:
    """Render the generic skill card template (fallback)."""
    return render("skill_card.j2", **kwargs)


def get_skill_prompt(skill_id: str, **kwargs) -> str:
    """Render a per-skill template from ~/.alex/skills/prompts/{skill_id}.j2.

    Falls back to the generic skill_card.j2 if the per-skill template doesn't exist.
    """
    try:
        return render(f"{skill_id}.j2", **kwargs)
    except Exception:
        return get_skill_card(**kwargs)


def save_skill_template(skill_id: str, name: str, pattern: str, instruction: str) -> None:
    """Create or overwrite a per-skill template file in ~/.alex/skills/prompts/."""
    content = _skill_template_content(name=name, pattern=pattern, instruction=instruction)
    path = SKILLS_PROMPT_DIR / f"{skill_id}.j2"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def remove_skill_template(skill_id: str) -> None:
    """Remove a per-skill template file from ~/.alex/skills/prompts/."""
    path = SKILLS_PROMPT_DIR / f"{skill_id}.j2"
    if path.exists():
        path.unlink()


def _skill_template_content(name: str, pattern: str, instruction: str) -> str:
    return f"## Skill: {name}\n**When**: {pattern}\n**How**: {instruction}\n"
