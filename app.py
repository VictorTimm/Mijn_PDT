"""ASGI entrypoint for Vercel.

The dashboard script is ``streamlit_app.py``. Vercel loads the top-level ``app``
object from this file. ``streamlit run app.py`` starts that dashboard directly.
"""

import streamlit as st


def _inside_streamlit() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        return get_script_run_ctx() is not None
    except Exception:
        return False


if _inside_streamlit():
    from streamlit_app import main

    main()
else:
    app = st.App("streamlit_app.py")

if __name__ == "__main__" and not _inside_streamlit():
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8501)
