from flask import Flask, render_template, request, redirect, url_for
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pandas as pd
import random
import string
from pydrive.auth import GoogleAuth
from pydrive.drive import GoogleDrive
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from io import StringIO

app = Flask(__name__)

# Google Sheets and Google Drive credentials setup
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
client = gspread.authorize(creds)

# Initialize Google Drive authentication
gauth = GoogleAuth()
gauth.credentials = creds
drive = GoogleDrive(gauth)


class MemberManager:
    def __init__(self, sheet_url, drive_folder_id):
        self.sheet_url = sheet_url
        self.spreadsheet = client.open_by_url(sheet_url)
        self.central_sheet = self.spreadsheet.sheet1
        self.drive_folder_id = drive_folder_id

    def generate_unique_id(self):
        while True:
            member_id = ''.join(random.choices(string.digits, k=5))
            if not self.central_sheet.findall(member_id):
                return member_id

    def add_member(self, name, position, team, number, email, facebook):
        member_id = self.generate_unique_id()
        member_data = [member_id, name, position, team, number, email, facebook]
        self.central_sheet.append_row(member_data)

        try:
            team_sheet = self.spreadsheet.worksheet(team)
        except gspread.exceptions.WorksheetNotFound:
            team_sheet = self.spreadsheet.add_worksheet(title=team, rows="100", cols="20")
        team_sheet.append_row(member_data)

        member_folder_name = f"{member_id}_{name}"
        member_folder = drive.CreateFile({'title': member_folder_name, 'parents': [{'id': self.drive_folder_id}],
                                          'mimeType': 'application/vnd.google-apps.folder'})
        member_folder.Upload()

        csv_filename = f"{name}.csv"
        csv_content = pd.DataFrame([member_data],
                                   columns=['ID', 'Name', 'Position', 'Team', 'Number', 'Email', 'Facebook ID']).to_csv(
            index=False)
        csv_file = drive.CreateFile({'title': csv_filename, 'parents': [{'id': member_folder['id']}]})
        csv_file.SetContentString(csv_content)
        csv_file.Upload()

        print(f"Member '{name}' added successfully.")

    def delete_member(self, identifier):
        try:
            if identifier.isdigit():
                cell = self.central_sheet.find(identifier)
            else:
                cell = self.central_sheet.find(identifier, in_column=2)
            row = cell.row
        except gspread.exceptions.CellNotFound:
            print(f"Member with {identifier} not found.")
            return

        member_data = self.central_sheet.row_values(row)
        self.central_sheet.delete_rows(row)

        team_name = member_data[3]
        try:
            team_sheet = self.spreadsheet.worksheet(team_name)
            team_cell = team_sheet.find(identifier)
            team_sheet.delete_rows(team_cell.row)
        except gspread.exceptions.WorksheetNotFound:
            print(f"Team sheet '{team_name}' not found.")
        except gspread.exceptions.CellNotFound:
            print(f"Member with {identifier} not found in team sheet '{team_name}'.")

        member_folder_name = f"{member_data[0]}_{member_data[1]}"
        member_folders = drive.ListFile(
            {'q': f"title='{member_folder_name}' and '{self.drive_folder_id}' in parents and trashed=false"}).GetList()

        if not member_folders:
            print(f"Member folder '{member_folder_name}' not found in Google Drive.")
            return

        member_folder = member_folders[0]
        removed_members_folder_name = "Removed Members"
        removed_members_folders = drive.ListFile({
            'q': f"title='{removed_members_folder_name}' and '{self.drive_folder_id}' in parents and trashed=false"}).GetList()

        if not removed_members_folders:
            removed_members_folder = drive.CreateFile(
                {'title': removed_members_folder_name, 'parents': [{'id': self.drive_folder_id}],
                 'mimeType': 'application/vnd.google-apps.folder'})
            removed_members_folder.Upload()
        else:
            removed_members_folder = removed_members_folders[0]

        try:
            member_folder_title = member_folder['title']
            member_folder_id = member_folder['id']
            parent_ids = ','.join([p['id'] for p in member_folder['parents']])
            drive_service = drive.auth.service
            drive_service.parents().delete(fileId=member_folder_id, parentId=parent_ids).execute()
            member_folder['parents'] = [{'id': removed_members_folder['id']}]
            member_folder.Upload()
            print(f"Member '{member_data[1]}' (ID: {member_data[0]}) deleted successfully.")
            print(f"Member folder '{member_folder_title}' moved to 'Removed Members' folder.")
        except Exception as e:
            print(f"Error moving member folder to 'Removed Members' folder: {e}")

    def assign_task(self, assigner_id, assign_to_id, task_data):
        assigner_name = self.get_member_name(assigner_id)
        assigner_position = self.get_member_position(assigner_id)
        assigner_team = self.get_member_team(assigner_id)
        assign_to_email = self.get_member_email(assign_to_id)
        member_name = self.get_member_name(assign_to_id)
        if not assign_to_email:
            print(f"Member with ID '{assign_to_id}' not found or has no associated email address.")
            return

        task_data.update({
            'Assign by': f"{assigner_id} ({assigner_name})",
            'Assign to': f"{assign_to_id} ({member_name})"
        })

        self.append_task_data('Task Assign', task_data)
        self.append_task_data_to_csv(assign_to_id, member_name, task_data)
        self.send_task_assignment_email(assign_to_id, assigner_name, assigner_position, assigner_team, task_data)
        print("Task assigned successfully.")

    def append_task_data(self, task_type, task_data):
        central_csv_filename = f"{task_type}.csv"
        query = f"title='{central_csv_filename}' and '{self.drive_folder_id}' in parents and trashed=false"
        file_list = drive.ListFile({'q': query}).GetList()

        if file_list:
            central_csv_file = file_list[0]
            central_csv_content = central_csv_file.GetContentString()
            central_csv_df = pd.read_csv(StringIO(central_csv_content))
        else:
            central_csv_df = pd.DataFrame()
            central_csv_file = drive.CreateFile(
                {'title': central_csv_filename, 'parents': [{'id': self.drive_folder_id}], 'mimeType': 'text/csv'})

        task_data_df = pd.DataFrame([task_data])
        central_csv_df = pd.concat([central_csv_df, task_data_df], ignore_index=True)
        central_csv_content = central_csv_df.to_csv(index=False)
        central_csv_file.SetContentString(central_csv_content)
        central_csv_file.Upload()

    def append_task_data_to_csv(self, member_id, member_name, task_data):
        member_folder = self.get_member_folder(member_id)
        if not member_folder:
            print(f"Member folder for ID '{member_id}' not found.")
            return

        csv_filename = f"{member_id}_{member_name}.csv"
        csv_path = f"{member_folder['title']}/{csv_filename}"
        query = f"title='{csv_filename}' and '{member_folder['id']}' in parents and trashed=false"
        file_list = drive.ListFile({'q': query}).GetList()

        if file_list:
            member_csv_file = file_list[0]
            member_csv_content = member_csv_file.GetContentString()
            member_csv_df = pd.read_csv(StringIO(member_csv_content))
        else:
            member_csv_df = pd.DataFrame()
            member_csv_file = drive.CreateFile(
                {'title': csv_filename, 'parents': [{'id': member_folder['id']}], 'mimeType': 'text/csv'})

        task_data_df = pd.DataFrame([task_data])
        member_csv_df = pd.concat([member_csv_df, task_data_df], ignore_index=True)
        member_csv_content = member_csv_df.to_csv(index=False)
        member_csv_file.SetContentString(member_csv_content)
        member_csv_file.Upload()
        print(f"Task assigned successfully for member ID '{member_id}'.")

    def send_task_assignment_email(self, assign_to_id, assign_by_name, assign_by_position, assign_by_team, task_data):
        assign_to_email = self.get_member_email(assign_to_id)
        member_name = self.get_member_name(assign_to_id)
        assign_by_email = self.get_member_email(assign_by_name)  # Assuming assign_by_name is the ID of the assigner

        if not assign_to_email:
            print(f"Member with ID '{assign_to_id}' not found or has no associated email address.")
            return

        if not assign_by_email:
            print(f"Assigner with ID '{assign_by_name}' not found or has no associated email address.")
            return

        smtp_server = 'smtp.gmail.com'
        port = 587
        sender_email = 'studentsoftwarearham@gmail.com'
        password = 'bjnneiyvvobjqkmm'

        message = MIMEMultipart()
        message['From'] = sender_email
        message['To'] = assign_to_email
        message['Subject'] = f"Task Assignment: {task_data['Task']}"

        body = f"Dear {member_name},\n\n" \
               f"You have been assigned a new task by {assign_by_name} ({assign_by_position}, {assign_by_team}).\n\n" \
               f"Task: {task_data['Task']}\n" \
               f"Description: {task_data['Description']}\n" \
               f"Deadline: {task_data['Deadline']}\n" \
               f"Priority: {task_data['Priority']}\n\n" \
               f"Best regards,\n" \
               f"Task Management System\n"

        message.attach(MIMEText(body, 'plain'))

        try:
            server = smtplib.SMTP(smtp_server, port)
            server.starttls()
            server.login(sender_email, password)
            server.sendmail(sender_email, assign_to_email, message.as_string())
            server.quit()
            print("Email notification sent successfully.")
        except Exception as e:
            print(f"Error sending email notification: {e}")

    def get_member_email(self, member_id):
        try:
            cell = self.central_sheet.find(member_id)
            row = cell.row
            return self.central_sheet.cell(row, 6).value
        except gspread.exceptions.CellNotFound:
            return None

    def get_member_name(self, member_id):
        try:
            cell = self.central_sheet.find(member_id)
            row = cell.row
            return self.central_sheet.cell(row, 2).value
        except gspread.exceptions.CellNotFound:
            return None

    def get_member_position(self, member_id):
        try:
            cell = self.central_sheet.find(member_id)
            row = cell.row
            return self.central_sheet.cell(row, 3).value
        except gspread.exceptions.CellNotFound:
            return None

    def get_member_team(self, member_id):
        try:
            cell = self.central_sheet.find(member_id)
            row = cell.row
            return self.central_sheet.cell(row, 4).value
        except gspread.exceptions.CellNotFound:
            return None

    def get_member_folder(self, member_id):
        member_folder_name = f"{member_id}_{self.get_member_name(member_id)}"
        member_folders = drive.ListFile(
            {'q': f"title='{member_folder_name}' and '{self.drive_folder_id}' in parents and trashed=false"}).GetList()
        if member_folders:
            return member_folders[0]
        return None

    def view_all_members(self):
        member_data = self.central_sheet.get_all_records()
        return member_data


manager = MemberManager(
    "https://docs.google.com/spreadsheets/d/19uBSdFE6yxLQG3TeB56B7UoI8BdN3X8UvlIvRyKo6lc/edit?usp=sharing",
    "1sizii9neOQMc6xlUATuCf7CbPjl7NvX2")


class MeetingManager:
    def __init__(self, sheet_url, drive_folder_id):
        self.sheet_url = sheet_url
        self.spreadsheet = client.open_by_url(sheet_url)
        self.meeting_sheet = self.spreadsheet.worksheet("Meetings")
        self.drive_folder_id = drive_folder_id

    def create_meeting(self, title, date, time, location, description, invitee_ids):
        meeting_id = ''.join(random.choices(string.digits, k=5))
        meeting_data = [meeting_id, title, date, time, location, description]
        self.meeting_sheet.append_row(meeting_data)

        for invitee_id in invitee_ids:
            self.send_meeting_invitation(meeting_id, title, date, time, location, description, invitee_id)

        print(f"Meeting '{title}' created successfully.")

    def send_meeting_invitation(self, meeting_id, title, date, time, location, description, invitee_id):
        invitee_email = manager.get_member_email(invitee_id)
        invitee_name = manager.get_member_name(invitee_id)

        if not invitee_email:
            print(f"Member with ID '{invitee_id}' not found or has no associated email address.")
            return

        smtp_server = 'smtp.gmail.com'
        port = 587
        sender_email = 'studentsoftwarearham@gmail.com'
        password = 'bjnneiyvvobjqkmm'

        message = MIMEMultipart()
        message['From'] = sender_email
        message['To'] = invitee_email
        message['Subject'] = f"Meeting Invitation: {title}"

        body = f"Dear {invitee_name},\n\n" \
               f"You are invited to a meeting. Please find the details below:\n\n" \
               f"Title: {title}\n" \
               f"Date: {date}\n" \
               f"Time: {time}\n" \
               f"Location: {location}\n" \
               f"Description: {description}\n\n" \
               f"Please confirm your attendance.\n\n" \
               f"Best regards,\n" \
               f"Meeting Organizer\n"

        message.attach(MIMEText(body, 'plain'))

        try:
            server = smtplib.SMTP(smtp_server, port)
            server.starttls()
            server.login(sender_email, password)
            server.sendmail(sender_email, invitee_email, message.as_string())
            server.quit()
            print("Email notification sent successfully.")
        except Exception as e:
            print(f"Error sending email notification: {e}")


meeting_manager = MeetingManager(
    "https://docs.google.com/spreadsheets/d/19uBSdFE6yxLQG3TeB56B7UoI8BdN3X8UvlIvRyKo6lc/edit?usp=sharing",
    "1sizii9neOQMc6xlUATuCf7CbPjl7NvX2")


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/meeting', methods=['GET', 'POST'])
def meeting():
    if request.method == 'POST':
        title = request.form['title']
        date = request.form['date']
        time = request.form['time']
        location = request.form['location']
        description = request.form['description']
        invitee_ids = request.form.getlist('invitee_ids')
        meeting_manager.create_meeting(title, date, time, location, description, invitee_ids)
        return redirect(url_for('meeting'))
    members = manager.view_all_members()
    return render_template('meeting.html', members=members)


@app.route('/call_meeting', methods=['POST'])
def call_meeting():
    caller_id = request.form['caller_id']
    audience = request.form['audience']
    date = request.form['date']
    time = request.form['time']
    zoom_link = request.form['zoom_link']
    agenda = request.form['agenda']
    contact_person = request.form['contact_person']
    contact_info = request.form['contact_info']

    if audience == 'All':
        invitee_ids = [member['ID'] for member in manager.view_all_members()]
    else:
        invitee_ids = [member['ID'] for member in manager.view_all_members() if member['Team'] == audience]

    meeting_title = f"Meeting called by {caller_id}"
    description = f"Agenda: {agenda}\nZoom Link: {zoom_link}\nContact Person: {contact_person}\nContact Info: {contact_info}"

    meeting_manager.create_meeting(meeting_title, date, time, 'Online', description, invitee_ids)
    return redirect(url_for('index'))


@app.route('/work', methods=['GET', 'POST'])
def work():
    if request.method == 'POST':
        assigner_id = request.form['assigner_id']
        assign_to_id = request.form['assign_to_id']
        task_name = request.form['task_name']
        deadline = request.form['deadline']
        task_details = request.form['task_details']
        task_priority = request.form['task_priority']
        comment = request.form['comment']

        task_data = {
            'Task': task_name,
            'Description': task_details,
            'Deadline': deadline,
            'Priority': task_priority,
            'Comment': comment
        }
        manager.assign_task(assigner_id, assign_to_id, task_data)
        return redirect(url_for('work'))
    return render_template('work.html')


@app.route('/members')
def members():
    all_members = manager.view_all_members()
    return render_template('members.html', members=all_members)


@app.route('/delete_member', methods=['POST'])
def delete_member():
    identifier = request.form['identifier']
    manager.delete_member(identifier)
    return redirect(url_for('members'))


@app.route('/update_member', methods=['POST'])
def update_member():
    identifier = request.form['identifier']
    name = request.form.get('name')
    position = request.form.get('position')
    team = request.form.get('team')
    number = request.form.get('number')
    email = request.form.get('email')
    facebook = request.form.get('facebook')

    member = manager.get_member_name(identifier)
    if not member:
        print(f"Member with ID or Name '{identifier}' not found.")
        return redirect(url_for('members'))

    # Implement logic to update member details in the Google Sheet
    # Example:
    cell = manager.central_sheet.find(identifier)
    row = cell.row
    if name:
        manager.central_sheet.update_cell(row, 2, name)
    if position:
        manager.central_sheet.update_cell(row, 3, position)
    if team:
        manager.central_sheet.update_cell(row, 4, team)
    if number:
        manager.central_sheet.update_cell(row, 5, number)
    if email:
        manager.central_sheet.update_cell(row, 6, email)
    if facebook:
        manager.central_sheet.update_cell(row, 7, facebook)

    print(f"Member '{identifier}' updated successfully.")
    return redirect(url_for('members'))


if __name__ == '__main__':
    app.run(debug=True)
