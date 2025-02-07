# Copyright © 2019 Province of British Columbia
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The unique worker functionality for this service is contained here.

The entry-point is the **cb_subscription_handler**

The design and flow leverage a few constraints that are placed upon it
by NATS Streaming and using AWAIT on the default loop.
- NATS streaming queues require one message to be processed at a time.
- AWAIT on the default loop effectively runs synchronously

If these constraints change, the use of Flask-SQLAlchemy would need to change.
Flask-SQLAlchemy currently allows the base model to be changed, or reworking
the model to a standalone SQLAlchemy usage with an async engine would need
to be pursued.
"""
import json
from typing import Dict

from flask import Flask
from legal_api.core import Filing as FilingCore
from legal_api.models import Business, Filing
from sqlalchemy_continuum import versioning_manager

from .filing_meta import FilingMeta, json_serial
from .filing_processors import registration


def get_filing_types(legal_filings: dict):
    """Get the filing type fee codes for the filing.

    Returns: {
        list: a list of filing types.
    }
    """
    filing_types = []
    for k in legal_filings['filing'].keys():
        if Filing.FILINGS.get(k, None):
            filing_types.append(k)
    return filing_types


def process_filing(filing_id: int, filing_event_data: Dict, db: any):
    """Render the filings contained in the submission.

    Start the migration to using core/Filing
    """

    filing_submission = Filing.find_by_id(filing_id)
    filing_core_submission = FilingCore.find_by_id(filing_id)


    if not filing_core_submission:
        raise Exception('not filing_core_submission')

    filing_submission = filing_core_submission.storage

    if filing_core_submission.status == Filing.Status.COMPLETED:
        print(f"""QueueFiler: Attempting to reprocess business.id={filing_submission.business_id}, 
                  filing.id={filing_submission.id}""")
        return None, None

    # convenience flag to set that the envelope is a correction
    is_correction = (filing_core_submission.filing_type == FilingCore.FilingTypes.CORRECTION)

    if legal_filings := filing_core_submission.legal_filings(with_diff=False):
        uow = versioning_manager.unit_of_work(db.session)
        transaction = uow.create_transaction(db.session)

        business = Business.find_by_internal_id(filing_submission.business_id)

        filing_meta = FilingMeta(application_date=filing_submission.effective_date,
                                 legal_filings=[item for sublist in
                                                [list(x.keys()) for x in legal_filings]
                                                for item in sublist])
        if is_correction:
            filing_meta.correction = {}

        for filing in legal_filings:

            if filing.get('registration'):
                business, filing_submission, filing_meta = registration.process(business,
                                                                                filing_core_submission.json,
                                                                                filing_submission,
                                                                                filing_meta,
                                                                                filing_event_data)

            # elif filing.get('changeOfRegistration'):
            #     change_of_registration.process(business, filing_submission, filing, filing_meta)
            #

        filing_submission.transaction_id = transaction.id
        filing_submission.set_processed()
        filing_submission._meta_data = json.loads(  # pylint: disable=W0212
            json.dumps(filing_meta.asjson, default=json_serial)
        )

        db.session.add(business)
        db.session.add(filing_submission)
        db.session.commit()

        #
        # if any('changeOfRegistration' in x for x in legal_filings):
        #     change_of_registration.post_process(business, filing_submission)
        #     AccountService.update_entity(
        #         business_registration=business.identifier,
        #         business_name=business.legal_name,
        #         corp_type_code=business.legal_type
        #     )
        #

        if any('registration' in x for x in legal_filings):
            filing_submission.business_id = business.id
            db.session.add(filing_submission)
            db.session.commit()
            registration.update_affiliation(business, filing_submission)
