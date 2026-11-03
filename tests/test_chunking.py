from app.chunking import chunk_document


def test_heading_hierarchy_and_code_block_are_preserved():
    content = "# Guide\n\nIntro.\n\n## First\n\n```sh\n# This is code, not a heading\necho test\n```\n\n## Second\n\nSecond section body."
    chunks = chunk_document(content, "markdown", "Title")
    assert [c["heading"] for c in chunks] == ["Guide", "Guide / First", "Guide / Second"]
    assert "# This is code, not a heading" in chunks[1]["text"]
    assert "First" not in chunks[2]["heading"]


def test_html_navigation_and_scripts_do_not_enter_index():
    content = '<html><nav>SECRET_NAV</nav><main><h1>Backup</h1><p>Real evidence.</p><script>EXECUTABLE</script><pre>pg_dump -j 2</pre></main></html>'
    chunks = chunk_document(content, "html", "Title")
    assert "SECRET_NAV" not in str(chunks)
    assert "EXECUTABLE" not in str(chunks)
    assert "pg_dump -j 2" in str(chunks)


def test_oversized_paragraph_does_not_drop_its_tail():
    chunks = chunk_document(" ".join(f"word{i}" for i in range(500)), "text", "Long")
    assert all(len(c["text"].split()) <= 150 for c in chunks)
    assert "word499" in chunks[-1]["text"]
