"""ASGI entrypoint for Vercel.

The dashboard script is ``streamlit_app.py``. Vercel loads the top-level ``app``
object from this file and serves every request through it.
"""

import streamlit as st

app = st.App("streamlit_app.py")

if __name__ == "__main__":
    app.run()
