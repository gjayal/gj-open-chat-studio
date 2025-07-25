# Pipeline File Upload Issue: "system: text content blocks must be non-empty"

## Problem Analysis

The error "system: text content blocks must be non-empty" occurs when the LLM node receives an empty system message after processing the prompt template. This happens because:

1. **Empty temp_state values**: When no files are uploaded, the Python node was setting `file_contents` to empty strings or placeholder text
2. **Template formatting**: The LLM prompt template uses `{temp_state.file_contents}` which gets formatted into the system message
3. **Claude API requirement**: Claude requires system messages to have non-empty content

## Root Cause

The issue is in the interaction between:
- **Python Node**: Sets `file_contents` in temp_state (sometimes to empty values)
- **LLM Node**: Uses `{temp_state.file_contents}` in the prompt template
- **Template Processing**: When `file_contents` is empty/None, the formatted system message becomes empty

## Solution

### 1. Improved Python Node (file_processing_node.py)

```python
def main(input: str, **kwargs) -> str: 
    # Get uploaded files from temp state
    attachments = get_temp_state_key("attachments")
    if not attachments:
        # Don't set file_contents at all when no files are uploaded
        # This allows the LLM prompt to handle the absence gracefully
        return input

    # Read all files and combine their contents
    all_file_contents = []
    for i, attachment in enumerate(attachments):
        try:
            filename = attachment.name if hasattr(attachment, 'name') else f"File {i+1}"
            file_ext = filename.lower().split('.')[-1] if '.' in filename else ''
            
            if file_ext in ['xlsx', 'xls']:
                all_file_contents.append(f"## {filename}\nExcel file uploaded (binary content cannot be displayed as text)")
            else:
                file_content = attachment.read_text()
                if file_content and file_content.strip():
                    all_file_contents.append(f"## {filename}\n{file_content}")
                else:
                    all_file_contents.append(f"## {filename}\n(File appears to be empty)")
        except Exception as e:
            filename = getattr(attachment, 'name', f'File {i+1}')
            all_file_contents.append(f"## {filename}\nError reading file: {str(e)}")

    # Save combined contents to temp_state only if we have actual content
    if all_file_contents:
        combined_contents = "\n\n".join(all_file_contents)
        # Only set if we have meaningful content
        if combined_contents.strip():
            set_temp_state_key("file_contents", combined_contents)
    
    return input
```

**Key Changes:**
- Don't set `file_contents` when no files are uploaded
- Only set `file_contents` when there's actual readable content
- Avoid creating placeholder text that could cause empty content blocks

### 2. Improved LLM Prompt Template

Use conditional templating to only include the file section when files exist:

```jinja2
{% if temp_state.file_contents %}
## Uploaded Files
{temp_state.file_contents}
{% endif %}
```

**Key Changes:**
- Use Jinja2 conditional `{% if temp_state.file_contents %}` to check if the key exists and has content
- Only render the "Uploaded Files" section when there are actual files
- Prevents empty content blocks in the system message

## Alternative Approaches

### Option A: Always Set a Default Value
If you prefer to always have `file_contents` set, use a meaningful default:

```python
# In Python node
if not attachments:
    set_temp_state_key("file_contents", "No files uploaded")
```

```jinja2
<!-- In LLM prompt -->
{% if temp_state.file_contents and temp_state.file_contents != "No files uploaded" %}
## Uploaded Files
{temp_state.file_contents}
{% endif %}
```

### Option B: Use a Different Key Name
Use a more specific key to avoid conflicts:

```python
# In Python node
if attachments:
    set_temp_state_key("uploaded_file_contents", combined_contents)
```

```jinja2
<!-- In LLM prompt -->
{% if temp_state.uploaded_file_contents %}
## Uploaded Files
{temp_state.uploaded_file_contents}
{% endif %}
```

## Testing Recommendations

1. **Test with no files**: Ensure the pipeline works when no files are uploaded
2. **Test with empty files**: Upload files with no content
3. **Test with various file types**: Excel, text, XML files
4. **Test error conditions**: Corrupted files, permission issues
5. **Monitor system messages**: Check that system messages are never empty

## Why This Approach Works

1. **Conditional rendering**: The Jinja2 template only includes file content when it exists
2. **No fake content**: We don't create placeholder text that could confuse the LLM
3. **Graceful degradation**: The system works whether files are uploaded or not
4. **API compliance**: Ensures system messages always have meaningful content

The key insight is to let the template system handle the absence of data rather than creating fake or empty content that could cause API violations.