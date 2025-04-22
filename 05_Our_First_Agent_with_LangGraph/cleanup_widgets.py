import json

notebook_path = "/Users/viveknatan/Documents/AIMCourse/agent/05_Our_First_Agent_with_LangGraph/Introduction_to_LangGraph_for_Agents_Assignment_Version.ipynb"

with open(notebook_path, "r", encoding="utf-8") as f:
    nb = json.load(f)

# Remove widget metadata if it exists
nb.get("metadata", {}).pop("widgets", None)

# Save the cleaned notebook
with open(notebook_path, "w", encoding="utf-8") as f:
    json.dump(nb, f, indent=2)
