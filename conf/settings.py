"""
    Class Name : Config

    Description:
        Contains all global config options needed for the bot to run

    Contributors:
        - Patrick Hennessy

    License:
        CL4M-B0T is free software: you can redistribute it and/or modify it
        under the terms of the GNU General Public License v3; as published
        by the Free Software Foundation
"""

import logging

class Config():

    # Name the bot will refer to itself; will change name on server if it is not this
    name = "CL4M-B0T"

    # Default command trigger. Messages that being with this are considered commands
    trigger = "."

    # Log level: https://docs.python.org/2/library/logging.html#logging-levels
    loglevel = logging.DEBUG

    # Connector to use
    connector = "discord"
    connectorOptions = {
        "email": "pmh35480@gmail.com",
        "password": "clambotpassword"
    }

    plugins = [
        "Chat",
    ]