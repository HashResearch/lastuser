# -*- coding: utf-8 -*-

"""
Adds support for texting Indian mobile numbers
"""

from datetime import datetime
from pytz import timezone
import requests
# from urllib2 import urlopen, URLError
# from urllib import urlencode

from flask import current_app, flash, request
from lastuser_core.models import db, SMSMessage, SMS_STATUS
from .. import lastuser_ui

# SMS GupShup sends delivery reports with this timezone
SMSGUPSHUP_TIMEZONE = timezone('Asia/Kolkata')


def send_message(msg):
    if msg.phone_number.startswith('+91'):  # Indian number. Use Exotel
        if len(msg.phone_number) != 13:
            raise ValueError("Invalid Indian mobile number")
        # All okay. Send!
        if not (current_app.config.get('SMS_EXOTEL_SID') and current_app.config.get('SMS_EXOTEL_TOKEN')):
            raise ValueError("Lastuser is not configured for SMS")
        else:
            sid = current_app.config['SMS_EXOTEL_SID']
            token = current_app.config['SMS_EXOTEL_TOKEN']
            r = requests.post('https://twilix.exotel.in/v1/Accounts/{sid}/Sms/send.json'.format(sid=sid),
                auth=(sid, token),
                data={
                    'From': current_app.config.get('SMS_FROM'),
                    'To': msg.phone_number,
                    'Body': msg.message
                })
            if r.status_code in (200, 201):
                # All good
                msg.transaction_id = r.json().get('SMSMessage', {}).get('Sid')
            else:
                # FIXME: This function should not be sending messages to the UI
                flash("Message could not be sent.", 'error')

        # # TODO: Also check if we have SMS GupShup credentials in settings.py
        # params = urlencode(dict(
        #     method='SendMessage',
        #     send_to=msg.phone_number[1:],  # Number with leading +
        #     msg=msg.message,
        #     msg_type='TEXT',
        #     format='text',
        #     v='1.1',
        #     auth_scheme='plain',
        #     userid=current_app.config['SMS_SMSGUPSHUP_USER'],
        #     password=current_app.config['SMS_SMSGUPSHUP_PASS'],
        #     mask=current_app.config['SMS_SMSGUPSHUP_MASK']
        #     ))
        # try:
        #     response = urlopen('https://enterprise.smsgupshup.com/GatewayAPI/rest?%s' % params).read()
        #     r_status, r_phone, r_id = [item.strip() for item in response.split('|')]
        #     if r_status == 'success':
        #         msg.status = SMS_STATUS.PENDING
        #         msg.transaction_id = r_id
        # except URLError, e:
        #     # FIXME: This function should not be sending messages to the UI
        #     flash("Message could not be sent. Error: %s" % e)
    else:
        # Unsupported at this time
        raise ValueError("Unsupported phone number")


def send_phone_verify_code(phoneclaim):
    msg = SMSMessage(phone_number=phoneclaim.phone,
        message=current_app.config['SMS_VERIFICATION_TEMPLATE'].format(code=phoneclaim.verification_code))
    # Now send this
    send_message(msg)
    db.session.add(msg)


@lastuser_ui.route('/report/smsgupshup')
def report_smsgupshup():
    externalId = request.args.get('externalId')
    deliveredTS = request.args.get('deliveredTS')
    status = request.args.get('status')
    phoneNo = request.args.get('phoneNo')
    cause = request.args.get('cause')

    # Find a corresponding message and ensure the parameters match
    msg = SMSMessage.query.filter_by(transaction_id=externalId).first()
    if not msg:
        return "No such message", 404
    elif msg.phone_number != '+' + phoneNo:
        return "Incorrect phone number", 404
    else:
        if status == 'SUCCESS':
            msg.status = SMS_STATUS.DELIVERED
        elif status == 'FAIL':
            msg.status = SMS_STATUS.FAILED
        else:
            msg.status == SMS_STATUS.UNKNOWN
        msg.fail_reason = cause
        if deliveredTS:
            deliveredTS = float(deliveredTS) / 1000.0
        # This delivery time is in IST, GMT+0530
        # Convert this into a naive UTC timestamp before saving
        local_status_at = datetime.fromtimestamp(deliveredTS)
        msg.status_at = local_status_at - SMSGUPSHUP_TIMEZONE.utcoffset(local_status_at)
    db.session.commit()
    return "Status updated"
