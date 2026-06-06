import logging

logger = logging.getLogger(__name__)

# Services to skip when restarting (always keep running)
SKIP_SERVICES = {"config", "traefik", "postgres", "nats", "frontend"}


def restart_services() -> dict:
    """Restart all HERD application services via Docker API.

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

    try:
        containers = client.containers.list(filters={"label": "com.docker.compose.service"})
    except Exception as exc:
        result["errors"].append(f"Cannot list containers: {exc}")
        return result

    for container in containers:
        service_name = container.labels.get("com.docker.compose.service", "")
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
