"""Общий HTTP/SSE-транспорт для настольного и консольного клиентов."""

import json

import httpx2


def connect(url, token):
    return httpx2.Client(
        base_url=url,
        headers={"Authorization": "Bearer " + token},
        timeout=httpx2.Timeout(20, connect=5),
        follow_redirects=False,
        trust_env=False,
    )


def post(client, path, kind, **data):
    response = client.post(path, json={"version": "0", "type": kind, **data})
    response.raise_for_status()
    return response.json()


def events(client, run, after=0, stopped=lambda: False):
    with client.stream("GET", f"/v0/runs/{run}/events", params={"after": after}) as stream:
        stream.raise_for_status()
        for line in stream.iter_lines():
            if stopped():
                return
            if line.startswith("data: "):
                event = json.loads(line[6:])
                yield event
                if event["type"] == "run.result":
                    return
