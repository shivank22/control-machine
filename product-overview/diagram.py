#!/usr/bin/env python3
"""High-level Control Machine architecture. Icons come from the diagrams library."""

from diagrams import Cluster, Diagram, Edge
from diagrams.onprem.client import Client, Users
from diagrams.onprem.container import Docker
from diagrams.onprem.database import PostgreSQL
from diagrams.programming.framework import FastAPI
from diagrams.programming.language import Python
from diagrams.saas.chat import Telegram

GRAPH_ATTR = {
    "fontsize": "20",
    "bgcolor": "white",
    "pad": "0.45",
    "splines": "spline",
}

with Diagram(
    "Control Machine",
    filename="architecture",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=GRAPH_ATTR,
):
    you = Users("You")

    with Cluster("Phone"):
        telegram = Telegram("Telegram")
        desktop = Client("Live desktop")

    with Cluster("Platform"):
        api = FastAPI("Factory")
        agent = Python("Agent")
        db = PostgreSQL("Postgres")

    with Cluster("Assigned computer"):
        connector = Client("Connector")
        chrome = Docker("Chrome")
        screen = Client("Clicks")

    you >> Edge(label="asks") >> telegram
    telegram >> Edge(label="message") >> api >> agent
    agent >> Edge(label="remembers") >> db
    connector >> Edge(label="dials out") >> api
    agent >> Edge(label="one session") >> connector
    connector >> Edge(label="drives") >> chrome
    connector >> Edge(label="clicks") >> screen
    you >> Edge(label="JPEG") >> desktop
