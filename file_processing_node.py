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

    # Save combined contents to temp state only if we have actual content
    if all_file_contents:
        combined_contents = "\n\n".join(all_file_contents)
        # Only set if we have meaningful content
        if combined_contents.strip():
            set_temp_state_key("file_contents", combined_contents)
    
    return input