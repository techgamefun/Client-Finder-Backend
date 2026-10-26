
def test_create_company(client):
    response = client.post(
        "/api/v1/companies",
        json={
            "name": "Example Manufacturing Pvt Ltd",
            "website": "https://example.com",
            "domain": "example.com",
            "industry": "Manufacturing",
            "city": "Kolkata",
            "state": "West Bengal",
        },
    )

    assert response.status_code == 201

    data = response.json()
    assert data["name"] == "Example Manufacturing Pvt Ltd"
    assert data["domain"] == "example.com"
    assert isinstance(data["id"], int)


def test_list_companies(client):
    client.post(
        "/api/v1/companies",
        json={
            "name": "Example Company",
            "domain": "example.com",
        },
    )

    response = client.get("/api/v1/companies")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["name"] == "Example Company"


def test_duplicate_domain_returns_conflict(client):
    payload = {
        "name": "Example Company",
        "domain": "example.com",
    }

    first_response = client.post(
        "/api/v1/companies",
        json=payload,
    )

    second_response = client.post(
        "/api/v1/companies",
        json={
            "name": "Another Company",
            "domain": "example.com",
        },
    )

    assert first_response.status_code == 201
    assert second_response.status_code == 409


def test_invalid_company_is_rejected(client):
    response = client.post(
        "/api/v1/companies",
        json={
            "name": "",
            "website": "not-a-url",
        },
    )

    assert response.status_code == 422
