"""Example second skill backend: an independently-operated partner hosting
its own skill payloads behind the same Envoy gateway and auth model as
registry_server. See envoy/backends.yaml (route_prefix: /partner/) and
skillward_common/skill_backend.py for how this is wired up.
"""

from pathlib import Path

from skillward_common.skill_backend import make_skill_backend_app

app = make_skill_backend_app(
    title="Skillward Partner Skill Backend",
    route_prefix="/partner",
    store_dir=Path(__file__).parent / "skills_store",
)
