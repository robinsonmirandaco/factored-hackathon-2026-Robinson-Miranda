"""TRAZO operator panel (step 1.18 placeholder).

Only shows API health and metrics so the compose stack has a working panel service.
The three tabs from docs/ui-spec are built in step 1.18.
"""

import os

import httpx
import streamlit as st

API_URL = os.getenv("API_URL", "http://localhost:8000")
TIMEOUT_SECONDS = 5.0

st.set_page_config(page_title="TRAZO", layout="wide")
st.title("TRAZO")

try:
    health = httpx.get(f"{API_URL}/health", timeout=TIMEOUT_SECONDS).json()
    metrics = httpx.get(f"{API_URL}/metrics", timeout=TIMEOUT_SECONDS).json()
except httpx.HTTPError as exc:
    st.error(f"API unreachable at {API_URL}: {type(exc).__name__}")
else:
    st.subheader("API health")
    st.json(health)
    st.subheader("Metrics")
    st.json(metrics)
