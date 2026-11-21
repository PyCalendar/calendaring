The conformance suite no longer leaves an item behind in a collection it borrowed when a write timed out after the server had stored the item: it notes the item's UID before writing, not after.
