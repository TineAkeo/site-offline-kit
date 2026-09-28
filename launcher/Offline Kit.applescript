-- Launcher for the Offline Kit app. Compile into "Offline Kit.app" with:
--   osacompile -o "Offline Kit.app" "launcher/Offline Kit.applescript"
-- A compiled applet (unlike a bare shell-script bundle) can ask macOS for
-- access to protected folders such as Downloads, so the kit can live there.
on run
	set appPath to POSIX path of (path to me)
	set kitPath to do shell script "dirname " & quoted form of appPath
	set sh to "export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH; cd " & quoted form of kitPath & " && "
	try
		do shell script sh & "command -v python3"
	on error
		display alert "Offline Kit needs Python 3" message "Install it from python.org, then open Offline Kit again."
		return
	end try
	try
		-- First access to a protected folder (e.g. Downloads): macOS asks here.
		do shell script sh & "head -c 1 kit_app.py > /dev/null && mkdir -p out"
	on error
		display alert "macOS blocked Offline Kit from its folder" message "Offline Kit needs access to " & kitPath & ". In System Settings → Privacy & Security → Files and Folders, allow Offline Kit to use this folder (e.g. Downloads), then open it again. Or move the site-offline-kit folder out of Downloads, Desktop and Documents."
		return
	end try
	-- Runs until the kit quits itself (a few minutes after its page is closed).
	do shell script sh & "python3 kit_app.py >> out/.kit_app.log 2>&1"
end run
