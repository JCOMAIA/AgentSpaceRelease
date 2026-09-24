"""Publishing without an authenticated HTTP client.

Three hosted chatbots were pointed at a live instance and none could publish:
browser chatbots produce text and, at best, perform a GET — none can send an
`Authorization` header. That excluded exactly the audience with no machine of
its own, which is the audience this exists for.

Two ways in, and the security of the second is the whole design. A GET that
published on its own would fire from chat link previews, from browser prefetch,
and from every crawler that follows a URL — and this product's links are meant
to be pasted into chats. So a chatbot can stage; only a signed-in person can
publish.
"""

from __future__ import annotations

import json

import pytest

from app import publish

CHATBOT_REPLY = """Claro! Aqui está uma página simples e bonita:

```html
<!doctype html>
<meta charset="utf-8">
<title>Meu Meme</title>
<h1>ha</h1>
```

Espero que goste! Se quiser eu mudo as cores."""


# --------------------------------------------------------------------------
# Reading whatever the human pasted
# --------------------------------------------------------------------------
def test_the_page_is_picked_out_of_the_whole_reply():
    """They will paste the entire answer. Asking them to trim it first is asking
    them to do the one step that makes this worse than using the API."""
    out = publish.extract_html(CHATBOT_REPLY)
    assert out.content.startswith("<!doctype html>")
    assert "Espero que goste" not in out.content
    assert "```" not in out.content


def test_the_filename_comes_from_the_page_title():
    assert publish.extract_html(CHATBOT_REPLY).name == "meu-meme.html"


def test_an_unlabelled_fence_still_works():
    out = publish.extract_html("aqui:\n\n```\n<h1>oi</h1>\n```\n")
    assert out.content.strip() == "<h1>oi</h1>"


def test_raw_html_with_no_fence_works():
    out = publish.extract_html("<!doctype html><title>Direto</title><p>x</p>")
    assert "Direto" in out.content
    assert out.name == "direto.html"


def test_plain_text_becomes_a_page_instead_of_an_error():
    """Someone with something they want at a URL should get a URL."""
    out = publish.extract_html("só um bilhete\ncom duas linhas")
    assert "<!doctype html>" in out.content
    assert "só um bilhete" in out.content
    assert out.note


def test_the_plain_text_wrapper_escapes_what_it_wraps():
    """Text that is not markup gets shown as text, not run as markup.

    Markup that *is* markup is published as-is on purpose — publishing a page
    with a script in it is the product. What contains it is the sandbox CSP on
    everything served, not this function second-guessing the author.
    """
    out = publish.extract_html("comparando: 5 < 10 & 3 > 1, sacou?")
    assert out.note
    assert "5 &lt; 10 &amp; 3 &gt; 1" in out.content


def test_real_markup_is_published_as_written():
    out = publish.extract_html(
        "<!doctype html><title>App</title><script>document.title='ok'</script>"
    )
    assert "<script>document.title='ok'</script>" in out.content
    assert not out.note


def test_an_empty_paste_says_what_to_do():
    from app.teaching import AgentSpaceError

    with pytest.raises(AgentSpaceError) as caught:
        publish.extract_html("   \n  ")
    assert "paste" in caught.value.fix.lower()


@pytest.mark.parametrize("given,expected", [
    ("meu site", "meu-site.html"),
    # The directory part is dropped entirely, so there is nothing left to escape with.
    ("../../etc/passwd", "passwd.html"),
    ("índex.html", "index.html"),
    ("a/b/c.html", "c.html"),
])
def test_requested_filenames_are_made_safe(given, expected):
    assert publish.normalise_name(given, "<h1>x</h1>") == expected


@pytest.mark.parametrize("title,expected", [
    ("Olá, Mundo", "ola-mundo.html"),
    ("Ação entre Amigos", "acao-entre-amigos.html"),
    ("Coração & Café", "coracao-cafe.html"),
    ("São Paulo", "sao-paulo.html"),
])
def test_accented_titles_keep_their_letters(title, expected):
    """Dropping the accented letter mangles almost every Portuguese title.

    "Olá, Mundo" produced `ol-mundo.html` before this: the á was removed rather
    than folded, taking the a with it.
    """
    assert publish.suggest_name(f"<title>{title}</title>") == expected


# --------------------------------------------------------------------------
# Paste to publish
# --------------------------------------------------------------------------
async def test_pasting_a_reply_puts_it_on_the_web(client, account):
    """The account fixture registers, which leaves a session cookie on the client."""
    res = await client.post("/publish", data={"pasted": CHATBOT_REPLY, "name": ""})
    assert res.status_code == 200, res.text
    assert "It is live" in res.text

    page = await client.get(f"/@{account['username']}/meu-meme.html")
    assert page.status_code == 200
    assert "ha" in page.text


async def test_the_name_can_be_chosen(client, account):
    await client.post("/publish", data={"pasted": CHATBOT_REPLY, "name": "index"})
    page = await client.get(f"/@{account['username']}/")
    assert page.status_code == 200
    assert "ha" in page.text


# --------------------------------------------------------------------------
# Editing, which is where iteration actually happens
# --------------------------------------------------------------------------
async def test_editing_keeps_the_same_address(client, account):
    """The link is already in someone's chat. It cannot move because the author
    changed a word in the title."""
    await client.post("/publish", data={"pasted": CHATBOT_REPLY, "name": ""})
    url = f"/@{account['username']}/meu-meme.html"
    assert "ha" in (await client.get(url)).text

    res = await client.post("/edit", data={
        "path": "public/meu-meme.html",
        "content": "<!doctype html><title>Outro Título Totalmente</title><h1>v2</h1>",
    })
    assert res.status_code == 200, res.text

    page = await client.get(url)
    assert page.status_code == 200
    assert "v2" in page.text
    # And no second page appeared under the new title.
    assert (await client.get(f"/@{account['username']}/outro-titulo-totalmente.html")
            ).status_code == 404


async def test_the_edit_box_holds_what_is_live(client, account):
    await client.post("/publish", data={"pasted": CHATBOT_REPLY})
    res = await client.get("/edit?path=public/meu-meme.html")
    assert res.status_code == 200
    assert "Meu Meme" in res.text


async def test_editing_someone_elses_page_is_not_possible(client, account):
    await client.post("/publish", data={"pasted": CHATBOT_REPLY})
    victim = account["username"]

    client.cookies.clear()
    await client.post("/api/v1/account/register", json={
        "username": "curioso", "email": "curioso@example.com",
        "password": "a-long-enough-password"})

    # Paths resolve inside the caller's own workspace, so this simply finds
    # nothing rather than reaching across.
    res = await client.get("/edit?path=public/meu-meme.html")
    assert res.status_code == 404
    assert (await client.get(f"/@{victim}/meu-meme.html")).status_code == 200


async def test_deleting_from_the_editor_takes_it_off_the_web(client, account):
    await client.post("/publish", data={"pasted": CHATBOT_REPLY})
    url = f"/@{account['username']}/meu-meme.html"
    assert (await client.get(url)).status_code == 200

    await client.post("/edit/delete", data={"path": "public/meu-meme.html"})
    assert (await client.get(url)).status_code == 404


async def test_publishing_over_an_existing_page_says_it_replaced_it(client, account):
    await client.post("/publish", data={"pasted": CHATBOT_REPLY})
    res = await client.post("/publish", data={"pasted": CHATBOT_REPLY})
    assert "Updated" in res.text


async def test_a_first_publish_does_not_claim_to_have_replaced_anything(client, account):
    res = await client.post("/publish", data={"pasted": CHATBOT_REPLY})
    assert "It is live" in res.text
    assert "Updated" not in res.text


async def test_the_owner_sees_edit_links_on_their_own_profile(client, account):
    await client.post("/publish", data={"pasted": CHATBOT_REPLY, "name": "algo.html"})
    res = await client.get(f"/@{account['username']}/")
    assert "/edit?path=public/algo.html" in res.text


async def test_a_visitor_sees_no_edit_links(client, account):
    await client.post("/publish", data={"pasted": CHATBOT_REPLY, "name": "algo.html"})
    owner = account["username"]
    client.cookies.clear()

    res = await client.get(f"/@{owner}/")
    assert res.status_code == 200
    assert "/edit?path=" not in res.text
    assert "Only you see this" not in res.text
    # Instructions the visitor cannot act on just make the page look unfinished.
    assert "profile.json" not in res.text


async def test_the_owner_is_told_how_to_set_up_their_profile(client, account):
    await client.post("/publish", data={"pasted": CHATBOT_REPLY, "name": "algo.html"})
    res = await client.get(f"/@{account['username']}/")
    assert "profile.json" in res.text


async def test_publishing_needs_a_signed_in_person(client):
    res = await client.post("/publish", data={"pasted": CHATBOT_REPLY}, follow_redirects=False)
    assert res.status_code == 302
    assert "/login" in res.headers["location"]


# --------------------------------------------------------------------------
# Drafts: what a chatbot with only GET can do
# --------------------------------------------------------------------------
async def get_token(client) -> str:
    res = await client.get("/publish/token")
    assert res.status_code == 200, res.text
    return res.json()["data"]["token"]


async def test_a_chatbot_can_stage_a_page_with_a_plain_get(client, account):
    token = await get_token(client)
    res = await client.get("/d/new", params={
        "t": token, "name": "oi.html", "c": "<!doctype html><title>Oi</title><h1>oi</h1>"})
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["draft_id"]
    assert data["confirm_url"].endswith(data["draft_id"])


async def test_a_staged_draft_is_not_on_the_web(client, account):
    """The property the whole design rests on."""
    token = await get_token(client)
    await client.get("/d/new", params={"t": token, "name": "oi.html", "c": "<h1>secreto</h1>"})

    page = await client.get(f"/@{account['username']}/oi.html")
    assert page.status_code == 404
    assert "secreto" not in page.text


async def test_a_person_publishes_it_and_then_it_is_live(client, account):
    token = await get_token(client)
    draft_id = (await client.get("/d/new", params={
        "t": token, "name": "oi.html",
        "c": "<!doctype html><title>Oi</title><h1>publicado</h1>"})).json()["data"]["draft_id"]

    res = await client.post(f"/d/{draft_id}/publish", data={"name": "oi.html"})
    assert res.status_code == 200, res.text

    page = await client.get(f"/@{account['username']}/oi.html")
    assert page.status_code == 200
    assert "publicado" in page.text


async def test_a_big_page_can_be_appended_across_several_gets(client, account):
    """A URL cannot carry a whole page; several can."""
    token = await get_token(client)
    draft_id = (await client.get("/d/new", params={
        "t": token, "c": "<!doctype html><title>Grande</title>"})).json()["data"]["draft_id"]

    for chunk in ["<style>body{color:red}</style>", "<h1>parte dois</h1>", "<p>fim</p>"]:
        res = await client.get(f"/d/{draft_id}/add", params={"t": token, "c": chunk})
        assert res.status_code == 200, res.text

    await client.post(f"/d/{draft_id}/publish", data={"name": "grande.html"})
    page = await client.get(f"/@{account['username']}/grande.html")
    assert "parte dois" in page.text and "fim" in page.text


async def test_the_review_page_shows_it_before_anything_is_published(client, account):
    token = await get_token(client)
    draft_id = (await client.get("/d/new", params={
        "t": token, "c": "<h1>revisar</h1>"})).json()["data"]["draft_id"]

    res = await client.get(f"/d/{draft_id}")
    assert res.status_code == 200
    assert "Publish it" in res.text
    # The preview must not be able to act as the person reviewing it.
    assert "sandbox" in res.text


# --------------------------------------------------------------------------
# What a draft token must not be able to do
# --------------------------------------------------------------------------
async def test_a_draft_token_cannot_publish(client, account):
    """Whoever holds it can only ever create work for the owner to look at."""
    token = await get_token(client)
    draft_id = (await client.get("/d/new", params={
        "t": token, "c": "<h1>x</h1>"})).json()["data"]["draft_id"]

    await client.get("/api/v1/account/logout")
    client.cookies.clear()

    res = await client.post(f"/d/{draft_id}/publish", data={"name": "x.html"},
                            follow_redirects=False)
    assert res.status_code == 302
    assert "/login" in res.headers["location"]


async def test_a_draft_token_is_not_a_session(client, account):
    """Scopes are separate, so a token that leaks through a chat log or a link
    preview cannot be replayed as the person who made it."""
    token = await get_token(client)
    client.cookies.clear()
    client.cookies.set("agentspace_session", token)

    res = await client.get("/dashboard", follow_redirects=False)
    assert res.status_code == 302, "a draft token opened a session"


async def test_a_session_cookie_is_not_a_draft_token(client, account):
    session = client.cookies.get("agentspace_session")
    assert session
    res = await client.get("/d/new", params={"t": session, "c": "<h1>x</h1>"})
    assert res.status_code == 401


@pytest.mark.parametrize("token", ["", "nonsense", "a.b", "ask_something"])
async def test_a_bad_token_is_refused_with_an_instruction(client, token):
    res = await client.get("/d/new", params={"t": token, "c": "<h1>x</h1>"})
    assert res.status_code == 401
    assert "/publish" in res.json()["error"]["fix"]


async def test_drafts_are_never_served_as_pages(client, account):
    """They live in a dot-directory, which hosting refuses outright."""
    token = await get_token(client)
    await client.get("/d/new", params={"t": token, "c": "<h1>rascunho</h1>"})

    for path in [".drafts", ".drafts/", "%2Edrafts"]:
        res = await client.get(f"/@{account['username']}/{path}")
        assert res.status_code == 404, path
        assert "rascunho" not in res.text


async def test_one_account_cannot_reach_another_accounts_draft(client, account):
    token = await get_token(client)
    draft_id = (await client.get("/d/new", params={
        "t": token, "c": "<h1>meu</h1>"})).json()["data"]["draft_id"]

    client.cookies.clear()
    other = await client.post("/api/v1/account/register", json={
        "username": "intruso", "email": "intruso@example.com",
        "password": "a-long-enough-password"})
    assert other.status_code == 200

    res = await client.get(f"/d/{draft_id}")
    assert res.status_code == 404


# --------------------------------------------------------------------------
# Where the key is allowed to arrive
# --------------------------------------------------------------------------
@pytest.mark.parametrize("header", ["x-api-key", "api-key", "apikey", "x-agentspace-key"])
async def test_the_key_is_accepted_under_any_of_the_usual_header_names(
    client, account, header
):
    """A hosted connector's setup dialog offers a menu of header names.

    Someone connecting Claude.ai picked `api-key`, we only read `x-api-key`,
    and the key was discarded in silence — the same intention spelled two ways.
    Refusing a valid credential over spelling is a puzzle with no lesson in it.
    """
    res = await client.get("/api/v1/whoami", headers={header: account["api_key"]})
    assert res.status_code == 200, res.text
    assert res.json()["data"]["username"] == account["username"]


async def test_a_key_pasted_into_authorization_without_bearer_still_works(client, account):
    res = await client.get("/api/v1/whoami", headers={"Authorization": account["api_key"]})
    assert res.status_code == 200, res.text


async def test_a_wrong_key_is_still_refused_whatever_header_it_arrives_in(client):
    res = await client.get("/api/v1/whoami", headers={"api-key": "ask_not_a_real_key"})
    assert res.status_code == 401


async def test_the_mcp_refusal_says_which_headers_it_saw(client):
    """Otherwise "needs a key" cannot be told apart from "key landed elsewhere"."""
    res = await client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream",
                 "X-Wrong-Header": "ask_somekey"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "whoami", "arguments": {}}},
    )
    text = json.dumps(res.json())
    assert "no credential header at all" in text
    assert "api-key" in text


async def test_the_mcp_refusal_names_the_header_that_did_arrive(client):
    res = await client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream",
                 "Authorization": "Bearer not-even-a-key-shape"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "whoami", "arguments": {}}},
    )
    assert "invalid_api_key" in json.dumps(res.json())
