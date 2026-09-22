#!/usr/bin/env python3
"""Backward-compatible alias. Prefer seed_agents.py """
import runpy

runpy.run_module("agents.seed_agents", run_name="__main__")
