$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
& 'C:\Users\jvben\Desktop\PROJECTS\_GLOBAL_AI_AUTOMATION\ai-v2.ps1' -Action apply -RepoPath $repo -Reason force
& 'C:\Users\jvben\Desktop\PROJECTS\_GLOBAL_AI_AUTOMATION\ai-v2.ps1' -Action capture -RepoPath $repo -Reason force
& 'C:\Users\jvben\Desktop\PROJECTS\_GLOBAL_AI_AUTOMATION\ai-v2.ps1' -Action refresh -RepoPath $repo -Reason force