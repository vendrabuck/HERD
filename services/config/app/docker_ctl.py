import logging
import socket

logger = logging.getLogger(__name__)

# Services to skip when restarting (always keep running)
SKIP_SERVICES = {"config", "traefik", "postgres", "nats", "frontend"}

PROJECT_LABEL = "com.docker.compose.project"
SERVICE_LABEL = "com.docker.compose.service"


def _own_compose_project(client) -> str | None:
    """Return the compose project of the container this code runs in.

    A container's default hostname is its own container id, so inspecting it
    yields our own labels. Returns None when the project cannot be determined
    (not running in a container, custom hostname, missing label); the caller
    must fail closed rather than restart unscoped, because a bare
    service-label filter matches every compose project on the host
    (issue #373: an apply restarted an unrelated stack).
    """
    try:
        me = client.containers.get(socket.gethostname())
    except Exception:
        return None
    return me.labels.get(PROJECT_LABEL) or None


def restart_services() -> dict:
    """Restart the HERD application services of this compose project via Docker API.

    Returns a dict with 'restarted' (list of names) and 'errors' (list of strings).
    """
    try:
        import docker
    except ImportError:
        return {"restarted": [], "errors": ["Docker SDK not installed"]}

    result = {"restarted": [], "errors": []}

    try:
        client = docker.from_env()
    except Exception as exc:
        result["errors"].append(f"Cannot connect to Docker: {exc}")
        return result

    project = _own_compose_project(client)
    if project is None:
        result["errors"].append(
            "Cannot determine this container's compose project; refusing an "
            "unscoped restart (it would hit every compose project on this host)"
        )
        return result

    try:
        containers = client.containers.list(
            filters={"label": [f"{PROJECT_LABEL}={project}", SERVICE_LABEL]}
        )
    except Exception as exc:
        result["errors"].append(f"Cannot list containers: {exc}")
        return result

    for container in containers:
        service_name = container.labels.get(SERVICE_LABEL, "")
        if service_name in SKIP_SERVICES:
            continue
        try:
            container.restart(timeout=30)
            result["restarted"].append(service_name)
            logger.info("Restarted service: %s", service_name)
        except Exception as exc:
            msg = f"Failed to restart {service_name}: {exc}"
            result["errors"].append(msg)
            logger.error(msg)

    return result
