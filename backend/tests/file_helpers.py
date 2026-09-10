"""Exercise the real shared file HTTP lifecycle in domain integration tests."""
def upload_file(client, filename, content, *, conversation_id=None, classification="supporting_evidence", description=None):
    if hasattr(content, "read"):
        content = content.read()
    response = client.post("/files", json={
        "purpose": "conversation" if conversation_id else "document",
        "conversation_id": str(conversation_id) if conversation_id else None,
        "filename": filename, "byte_size": len(content),
        "classification": classification, "description": description,
    })
    if response.status_code != 201:
        return response
    return client.post(f"/files/{response.json()['id']}/content", content=content,
                       headers={"Content-Type": "application/octet-stream"})
