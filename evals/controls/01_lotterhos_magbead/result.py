from opentrons import protocol_api

metadata = {'protocolName': 'bead_cleanup', 'author': 'Biolab AI', 'apiLevel': '2.15'}

def run(protocol: protocol_api.ProtocolContext):
    mod_4 = protocol.load_module('magnetic module gen2', '4')

    lw_3 = protocol.load_labware('nest_12_reservoir_15ml', '3', label='ethanol_reservoir')
    lw_1 = protocol.load_labware('opentrons_96_tiprack_300ul', '1', label='tiprack_300')
    lw_4 = mod_4.load_labware('nest_96_wellplate_2ml_deep', label='sample_plate')
    lw_2 = protocol.load_labware('opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap', '2', label='reagent_rack')
    lw_5 = protocol.load_labware('nest_96_wellplate_100ul_pcr_full_skirt', '5', label='elution_plate')

    pip_left = protocol.load_instrument('p300_single_gen2', 'left', tip_racks=[lw_1])

    pip_left.transfer(80.0, lw_2['A1'], lw_4['A1'])
    pip_left.pick_up_tip()
    pip_left.mix(3, 100.0, lw_4['A1'])
    pip_left.drop_tip()
    protocol.delay(minutes=5.0)
    mod_4.engage()
    protocol.pause('Wait until the liquid is clear (beads fully pelleted on magnet) before proceeding to supernatant removal.')
    pip_left.transfer(180.0, lw_4['A1'], lw_3['A1'])
    pip_left.transfer(200.0, lw_3['A1'], lw_4['A1'])
    protocol.delay(seconds=30.0)
    pip_left.transfer(200.0, lw_4['A1'], lw_3['A1'])
    pip_left.transfer(200.0, lw_3['A1'], lw_4['A1'])
    protocol.delay(seconds=30.0)
    pip_left.transfer(200.0, lw_4['A1'], lw_3['A1'])
    protocol.delay(minutes=3.0)
    mod_4.disengage()
    pip_left.transfer(30.0, lw_2['A2'], lw_4['A1'])
    pip_left.pick_up_tip()
    pip_left.mix(3, 50.0, lw_4['A1'])
    pip_left.drop_tip()
    protocol.delay(minutes=2.0)
    mod_4.engage()
    protocol.pause('Wait until the liquid is clear (eluate free of beads) before transferring to elution_plate.')
    pip_left.transfer(32.0, lw_4['A1'], lw_5['A1'])