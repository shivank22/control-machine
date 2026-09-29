#!/usr/bin/env python3
"""High-level Computer Bot architecture. Icons come from the diagrams library."""

from diagrams import Cluster, Diagram, Edge
from diagrams.onprem.client import Client, Users
from diagrams.onprem.database import PostgreSQL
from diagrams.onprem.network import Internet
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
    "Computer Bot",
    filename="architecture",
    outformat="png",
    show=False,
    direction="LR",
    graph_attr=GRAPH_ATTR,
):
    you = Users("You")

    with Cluster("Phone"):
        telegram = Telegram("Telegram")

    with Cluster("This computer"):
        api = FastAPI("Dashboard")
        supervisor = Python("Supervisor agent")
        browser = Python("Browser agent")
        screen = Client("File and desktop agents")
        chrome = Internet("Chrome")
        db = PostgreSQL("Postgres")

    you >> Edge(label="asks") >> telegram
    telegram >> Edge(label="message") >> api >> supervisor
    supervisor >> Edge(label="delegates") >> browser
    browser >> Edge(label="drives") >> chrome
    supervisor >> Edge(label="delegates") >> screen
    supervisor >> Edge(label="remembers") >> db
    you >> Edge(label="live link") >> chrome
