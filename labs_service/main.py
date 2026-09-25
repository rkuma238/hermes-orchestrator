"""Third skill backend, added purely by (1) writing this file + its
skills_store and (2) adding an entry to envoy/backends.yaml — no changes to
registry_server, partner_service, or hand-edited Envoy routes. Proves the
gateway scales past two backends via config, not code.
"""

from pathlib import Path

from osp_common.skill_backend import make_skill_backend_app

app = make_skill_backend_app(
    title="OSP Labs Skill Backend",
    route_prefix="/labs",
    store_dir=Path(__file__).parent / "skills_store",
)
