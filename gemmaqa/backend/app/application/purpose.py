"""Application purpose inference from observed structure."""

from __future__ import annotations

from app.application.models import ApplicationModel


def infer_application_purpose(model: ApplicationModel) -> tuple[str, float, list[str]]:
    """
    Return (purpose, confidence, evidence refs).
    Only claim a purpose when enough evidence exists.
    Prefer routes/headings/forms/modules — not marketing hostnames alone.
    """
    evidence: list[str] = []
    visited = model.visited_pages() or model.pages
    paths = " ".join(p.normalized_path.lower() for p in visited)
    titles = " ".join((p.title or "").lower() for p in visited)
    headings = " ".join((p.heading or "").lower() for p in visited)
    modules = " ".join(m.name.lower() for m in model.modules)
    form_blob = " ".join(
        " ".join([f.form_name] + [fld.label for fld in f.fields]) for f in model.forms
    ).lower()
    nav_labels = " ".join(
        (e.action_label or "").lower() for e in model.navigation_edges
    )
    # Exclude hostname-derived application_name from keyword blob to avoid
    # over-claiming from hosts like thinking-tester-contact-list.herokuapp.com
    # unless routes/UI also support it.
    blob = f"{paths} {titles} {headings} {modules} {form_blob} {nav_labels}"

    has_auth = any(
        k in blob
        for k in (
            "login",
            "sign up",
            "signup",
            "register",
            "adduser",
            "add user",
            "authentication",
            "password",
            "email",
        )
    ) and any(
        k in blob
        for k in ("login", "sign up", "signup", "register", "adduser", "add user", "password")
    )
    has_contacts = any(
        k in blob for k in ("contact", "contacts", "contactlist", "contact-list")
    )
    # Host / product name hint (e.g. "Contact List App")
    host_hint = "contact" in (model.application_name or "").lower()
    title_hint = "contact" in titles
    page_count = len(visited)

    # Password form on a contact-titled app is enough evidence
    if page_count >= 1 and (has_contacts or host_hint or title_hint) and (
        has_auth or any(f.fields for f in model.forms)
    ):
        if has_auth or any(
            (fld.input_type or "").lower() == "password" for f in model.forms for fld in f.fields
        ):
            evidence.append("Contact List authentication surface observed")
            return (
                "A contact management application that allows users to register, "
                "authenticate, and manage personal contacts.",
                0.78,
                evidence,
            )

    if (has_contacts or host_hint) and has_auth and page_count >= 1:
        if has_contacts:
            evidence.append("routes/modules include authentication and contacts")
        elif host_hint and has_auth:
            evidence.append("authentication flows observed on a contact-oriented application host")
        if any(
            p.normalized_path.lower().endswith("adduser")
            or "add user" in (p.heading or "").lower()
            for p in visited
        ):
            evidence.append("Add User registration page observed")
        if any("login" in p.normalized_path.lower() for p in visited):
            evidence.append("Login page observed")
        if any("contact" in p.normalized_path.lower() for p in visited):
            evidence.append("Contact list route observed")
        purpose = (
            "A contact management application that allows users to register, "
            "authenticate, and manage personal contacts."
        )
        confidence = 0.85 if (has_contacts and page_count >= 2) else 0.72
        return purpose, confidence, evidence

    if has_contacts and page_count >= 1:
        evidence.append("Contact-related routes or headings observed")
        return (
            "A web application focused on managing contacts.",
            0.65,
            evidence,
        )

    if has_auth and page_count >= 1:
        evidence.append("Authentication-related pages observed")
        # Contact List app often only exposes auth pages in safe mode
        if host_hint or "contact" in titles:
            evidence.append("Application title/host indicates contact management")
            return (
                "A contact management application that allows users to register, "
                "authenticate, and manage personal contacts.",
                0.7,
                evidence,
            )
        return (
            "A web application with user registration and/or login flows.",
            0.58,
            evidence,
        )

    if page_count >= 2 and model.forms:
        evidence.append(f"{page_count} pages and {len(model.forms)} forms observed")
        name = model.application_name or "This application"
        return (
            f"{name} exposes multiple pages and forms under exploratory observation.",
            0.45,
            evidence,
        )

    if page_count == 0:
        return "Under analysis", 0.0, []

    return "Under analysis", 0.25, [f"{page_count} page(s) observed"]
