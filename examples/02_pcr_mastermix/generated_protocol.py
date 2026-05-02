from opentrons import protocol_api

metadata = {'protocolName': 'pcr_setup', 'author': 'Biolab AI', 'apiLevel': '2.15'}

def run(protocol: protocol_api.ProtocolContext):
    lw_1 = protocol.load_labware('opentrons_96_tiprack_20ul', '1', label='tiprack_20')
    lw_3 = protocol.load_labware('nest_96_wellplate_100ul_pcr_full_skirt', '3', label='sample_plate')
    lw_6 = protocol.load_labware('nest_96_wellplate_100ul_pcr_full_skirt', '6', label='pcr_plate')
    lw_4 = protocol.load_labware('opentrons_96_tiprack_300ul', '4', label='tiprack_300')
    lw_2 = protocol.load_labware('opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap', '2', label='reagent_rack')

    pip_left = protocol.load_instrument('p20_single_gen2', 'left', tip_racks=[lw_1])

    pip_left.distribute(10.5, lw_2['A1'], [lw_6['A1'], lw_6['B1'], lw_6['C1'], lw_6['D1'], lw_6['E1'], lw_6['F1'], lw_6['G1'], lw_6['H1'], lw_6['A2'], lw_6['B2'], lw_6['C2'], lw_6['D2'], lw_6['E2'], lw_6['F2'], lw_6['G2'], lw_6['H2'], lw_6['A3'], lw_6['B3'], lw_6['C3'], lw_6['D3'], lw_6['E3'], lw_6['F3'], lw_6['G3'], lw_6['H3']])
    pip_left.transfer(2.0, lw_3['A1'], lw_6['A1'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['B1'], lw_6['B1'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['C1'], lw_6['C1'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['D1'], lw_6['D1'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['E1'], lw_6['E1'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['F1'], lw_6['F1'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['G1'], lw_6['G1'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['H1'], lw_6['H1'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['A2'], lw_6['A2'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['B2'], lw_6['B2'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['C2'], lw_6['C2'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['D2'], lw_6['D2'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['E2'], lw_6['E2'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['F2'], lw_6['F2'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['G2'], lw_6['G2'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['H2'], lw_6['H2'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['A3'], lw_6['A3'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['B3'], lw_6['B3'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['C3'], lw_6['C3'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['D3'], lw_6['D3'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['E3'], lw_6['E3'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['F3'], lw_6['F3'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['G3'], lw_6['G3'], mix_after=(3, 10.0))
    pip_left.transfer(2.0, lw_3['H3'], lw_6['H3'], mix_after=(3, 10.0))