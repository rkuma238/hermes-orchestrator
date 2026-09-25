"""Renders envoy/envoy.yaml from the declarative backend list in
envoy/backends.yaml. Run this after editing backends.yaml:

    python -m scripts.generate_envoy_config

Route ordering rule (Envoy matches top-to-bottom, first match wins):
  1. /internal/ is always blocked outright (never routed, ext_authz skipped).
  2. Each backend's declared public_paths (ext_authz explicitly disabled).
  3. Each backend's main route_prefix, longest prefix first, "/" always last,
     so a backend can't accidentally shadow a more specific one just by
     listing order in the YAML.
"""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parent.parent
BACKENDS_FILE = REPO_ROOT / "envoy" / "backends.yaml"
OUTPUT_FILE = REPO_ROOT / "envoy" / "envoy.yaml"

EXT_AUTHZ_DISABLED = {
    "envoy.filters.http.ext_authz": {
        "@type": "type.googleapis.com/envoy.extensions.filters.http.ext_authz.v3.ExtAuthzPerRoute",
        "disabled": True,
    }
}


def _routes(backends: list[dict]) -> list[dict]:
    routes = [
        {
            "match": {"prefix": "/internal/"},
            "direct_response": {"status": 404, "body": {"inline_string": "not found"}},
            "typed_per_filter_config": EXT_AUTHZ_DISABLED,
        }
    ]

    for backend in backends:
        for public_path in backend.get("public_paths", []):
            match = {"prefix": public_path["prefix"]}
            if public_path.get("method"):
                match["headers"] = [{"name": ":method", "string_match": {"exact": public_path["method"]}}]
            routes.append(
                {
                    "match": match,
                    "route": {"cluster": backend["name"]},
                    "typed_per_filter_config": EXT_AUTHZ_DISABLED,
                }
            )

    for backend in sorted(backends, key=lambda b: (b["route_prefix"] == "/", -len(b["route_prefix"]))):
        routes.append({"match": {"prefix": backend["route_prefix"]}, "route": {"cluster": backend["name"]}})

    return routes


def _cluster(backend: dict) -> dict:
    return {
        "name": backend["name"],
        "type": "STATIC",
        "connect_timeout": "1s",
        "lb_policy": "ROUND_ROBIN",
        "load_assignment": {
            "cluster_name": backend["name"],
            "endpoints": [
                {
                    "lb_endpoints": [
                        {
                            "endpoint": {
                                "address": {
                                    "socket_address": {"address": backend["host"], "port_value": backend["port"]}
                                }
                            }
                        }
                    ]
                }
            ],
        },
    }


def build_config(spec: dict) -> dict:
    backends = spec["backends"]
    authz_backends = [b for b in backends if b.get("authz_backend")]
    if len(authz_backends) != 1:
        raise ValueError("exactly one backend in backends.yaml must set authz_backend: true")
    authz_backend = authz_backends[0]

    return {
        "admin": {"address": {"socket_address": {"address": "127.0.0.1", "port_value": 9901}}},
        "static_resources": {
            "listeners": [
                {
                    "name": "gateway_listener",
                    "address": {"socket_address": {"address": "0.0.0.0", "port_value": 10000}},
                    "filter_chains": [
                        {
                            "filters": [
                                {
                                    "name": "envoy.filters.network.http_connection_manager",
                                    "typed_config": {
                                        "@type": "type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager",
                                        "stat_prefix": "gateway",
                                        "route_config": {
                                            "name": "local_route",
                                            "virtual_hosts": [
                                                {
                                                    "name": "registry_vhost",
                                                    "domains": ["*"],
                                                    "routes": _routes(backends),
                                                }
                                            ],
                                        },
                                        "http_filters": [
                                            {
                                                "name": "envoy.filters.http.ext_authz",
                                                "typed_config": {
                                                    "@type": "type.googleapis.com/envoy.extensions.filters.http.ext_authz.v3.ExtAuthz",
                                                    "failure_mode_allow": False,
                                                    "allowed_headers": {"patterns": [{"exact": "authorization"}]},
                                                    "http_service": {
                                                        "server_uri": {
                                                            "uri": f"http://{authz_backend['host']}:{authz_backend['port']}",
                                                            "cluster": authz_backend["name"],
                                                            "timeout": "0.5s",
                                                        },
                                                        "path_prefix": "/internal/authz",
                                                        "authorization_response": {
                                                            "allowed_upstream_headers": {
                                                                "patterns": [{"exact": "x-account-id"}]
                                                            }
                                                        },
                                                    },
                                                },
                                            },
                                            {
                                                "name": "envoy.filters.http.router",
                                                "typed_config": {
                                                    "@type": "type.googleapis.com/envoy.extensions.filters.http.router.v3.Router"
                                                },
                                            },
                                        ],
                                    },
                                }
                            ]
                        }
                    ],
                }
            ],
            "clusters": [_cluster(b) for b in backends],
        },
    }


def main() -> None:
    spec = yaml.safe_load(BACKENDS_FILE.read_text())
    config = build_config(spec)
    backend_summary = ", ".join(f"{b['name']} -> {b['route_prefix']}" for b in spec["backends"])
    header = (
        "# GENERATED FILE -- do not hand-edit. Source of truth: envoy/backends.yaml\n"
        "# Regenerate with: python -m scripts.generate_envoy_config\n"
        f"# Backends: {backend_summary}\n"
        "# Gateway listens on :10000. Envoy admin (stats only) on 127.0.0.1:9901.\n\n"
    )
    OUTPUT_FILE.write_text(header + yaml.safe_dump(config, sort_keys=False, default_flow_style=False))
    print(f"wrote {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
