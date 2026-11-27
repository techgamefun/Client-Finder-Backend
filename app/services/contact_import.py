from app.models import Contact, Website


def add_crawl_contacts(
    website: Website,
    emails: list[str],
    phones: list[str],
) -> int:
    existing_emails = {
        contact.business_email.casefold().strip()
        for contact in website.contacts
        if contact.business_email
    }
    existing_phones = {
        _phone_key(contact.business_phone)
        for contact in website.contacts
        if contact.business_phone
    }
    existing_phones.discard("")

    contacts_added = 0
    for email in emails:
        normalized_email = email.strip().casefold()
        if not normalized_email or normalized_email in existing_emails:
            continue
        website.contacts.append(
            Contact(
                organization_id=website.organization_id,
                business_email=normalized_email,
                is_publicly_listed=True,
            )
        )
        existing_emails.add(normalized_email)
        contacts_added += 1

    for phone in phones:
        normalized_phone = phone.strip()
        phone_key = _phone_key(normalized_phone)
        if not normalized_phone or not phone_key or phone_key in existing_phones:
            continue
        website.contacts.append(
            Contact(
                organization_id=website.organization_id,
                business_phone=normalized_phone,
                is_publicly_listed=True,
            )
        )
        existing_phones.add(phone_key)
        contacts_added += 1

    return contacts_added


def _phone_key(value: str) -> str:
    return "".join(character for character in value if character in "0123456789")
